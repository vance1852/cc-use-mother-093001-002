"""文化素材权利链核验域服务。

在基础服务的权限、幂等、事务与哈希审计边界上实现：

- 素材来源、权利人、共同创作份额、许可地域/用途/期限、必需署名登记；
- 证明文件只存摘要与 SHA-256，敏感证明对评委脱敏；
- 授权专员出具真实性结论、冲突复核员独立复核，结论只追加不改写；
- 作品版本进入评审时固化当时结论依据，事后补交/撤回只生成新结论；
- 许可失效时定位受影响版本、评审阶段与尚未签署的商业合作；
- 异议立案、证据保全、局部冻结、和解/裁定全流程。
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rights_models import (
    CommercialDeal,
    Dispute,
    Material,
    ReviewEntry,
    RightsConclusion,
    RightsHolder,
    VerificationTask,
    Work,
    WorkVersion,
)
from .service import DomainService

# 可以看到敏感证明摘要与完整结论依据的职责。
CLEARANCE_ROLES = frozenset({"admin", "auditor", "licensing_officer", "conflict_reviewer"})
# 评委类角色：只能看到脱敏后的证明信息。
JUDGING_ROLES = frozenset({"judge", "reviewer"})

USABLE_DECISIONS = frozenset({"cleared", "restricted"})
DECISIONS = frozenset({"cleared", "restricted", "blocked", "expired", "revoked", "withdrawn"})
MATERIAL_KINDS = frozenset({
    "photo", "rubbing", "oral_story", "manuscript", "artwork", "audio", "video", "other",
})
PURPOSE_PATTERN = None  # 用途为非空短字符串，具体清单由赛事约定


def _b64_hash(content_base64: str) -> tuple[str, int]:
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except Exception as exc:
        raise ValidationError("content_base64 必须是合法 Base64") from exc
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _parse_dt(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return parsed


class RightsService(DomainService):
    """权利链核验的全部写操作与受控读操作。"""

    # ------------------------------------------------------------------ 作品

    def register_work(self, *, request_id: str, actor_id: str, site_id: str,
                      work_id: str, title: str, primary_creator_id: str) -> Any:
        payload = {"actor_id": actor_id, "site_id": site_id, "work_id": work_id,
                   "title": title, "primary_creator_id": primary_creator_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "creator", "licensing_officer")
            site = conn.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的场所登记作品")
            creator = self._actor(conn, primary_creator_id)
            if creator.organization_id != site["organization_id"]:
                raise ValidationError("主创必须属于同组织")
            work_id = self._identifier(work_id, "work_id")
            title = self._text(title, "title")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO rworks(work_id,site_id,title,primary_creator_id,current_stage,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (work_id, site_id, title, primary_creator_id, "draft", self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("作品编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="rights_work.registered",
                             resource_type="rwork", resource_id=work_id,
                             detail={"site_id": site_id, "title": title,
                                     "primary_creator_id": primary_creator_id},
                             occurred_at=self._now())
                return "rwork", work_id, {"work_id": work_id}

            return self._idempotent(conn, request_id=request_id, action="rights_register_work",
                                    payload=payload, create=create)

    def get_work(self, work_id: str) -> Work:
        row = self.database.connection.execute("SELECT * FROM rworks WHERE work_id=?", (work_id,)).fetchone()
        if row is None:
            raise NotFoundError("作品不存在")
        return Work(row["work_id"], row["site_id"], row["title"], row["primary_creator_id"],
                    row["current_stage"], row["created_at"])

    # ------------------------------------------------------------- 权利人

    def register_rights_holder(self, *, request_id: str, actor_id: str, holder_id: str,
                               display_name: str, kind: str,
                               contact_summary: str, can_accept_channel_deals: bool,
                               share_percent: int = 0) -> Any:
        payload = {"actor_id": actor_id, "holder_id": holder_id, "display_name": display_name,
                   "kind": kind, "share_percent": share_percent,
                   "contact_summary": contact_summary,
                   "can_accept_channel_deals": can_accept_channel_deals}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "creator", "licensing_officer")
            holder_id = self._identifier(holder_id, "holder_id")
            display_name = self._text(display_name, "display_name")
            if kind not in {"holder", "coauthor", "both"}:
                raise ValidationError("kind 必须是 holder/coauthor/both")
            share_percent = int(share_percent)
            if not 0 <= share_percent <= 100:
                raise ValidationError("share_percent 必须在 0-100 之间")
            contact_summary = self._text(contact_summary, "contact_summary", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO rights_holders(holder_id,display_name,kind,share_percent,"
                        "contact_summary,can_accept_channel_deals,created_at) VALUES(?,?,?,?,?,?,?)",
                        (holder_id, display_name, kind, share_percent, contact_summary,
                         1 if can_accept_channel_deals else 0, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("权利人编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="rights_holder.registered",
                             resource_type="rights_holder", resource_id=holder_id,
                             detail={"display_name": display_name, "kind": kind,
                                     "share_percent": share_percent,
                                     "can_accept_channel_deals": bool(can_accept_channel_deals)},
                             occurred_at=self._now())
                return "rights_holder", holder_id, {"holder_id": holder_id}

            return self._idempotent(conn, request_id=request_id, action="rights_register_holder",
                                    payload=payload, create=create)

    def get_rights_holder(self, holder_id: str) -> RightsHolder:
        row = self.database.connection.execute(
            "SELECT * FROM rights_holders WHERE holder_id=?", (holder_id,)).fetchone()
        if row is None:
            raise NotFoundError("权利人不存在")
        return self._holder_from_row(row)

    @staticmethod
    def _holder_from_row(row) -> RightsHolder:
        return RightsHolder(row["holder_id"], row["display_name"], row["kind"],
                            row["share_percent"], row["contact_summary"],
                            bool(row["can_accept_channel_deals"]), row["created_at"])

    # --------------------------------------------------------------- 素材

    def register_material(self, *, request_id: str, actor_id: str, work_id: str, material_id: str,
                          kind: str, title: str, source_description: str,
                          rights_holder_ids: list[str], coauthor_ids: list[str],
                          territory: str, purposes: list[str], valid_from: str,
                          valid_until: str | None, required_attribution: str,
                          holder_shares: dict[str, int] | None = None,
                          alternative_material_id: str | None = None) -> Any:
        payload = {
            "actor_id": actor_id, "work_id": work_id, "material_id": material_id, "kind": kind,
            "title": title, "source_description": source_description,
            "rights_holder_ids": list(rights_holder_ids),
            "holder_shares": holder_shares, "coauthor_ids": list(coauthor_ids),
            "territory": territory, "purposes": list(purposes), "valid_from": valid_from,
            "valid_until": valid_until, "required_attribution": required_attribution,
            "alternative_material_id": alternative_material_id,
        }
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "creator", "licensing_officer")
            work = conn.execute("SELECT * FROM rworks WHERE work_id=?", (work_id,)).fetchone()
            if work is None:
                raise NotFoundError("作品不存在")
            if actor.role == "creator" and work["primary_creator_id"] != actor_id:
                raise PermissionDenied("只能为自己担任主创的作品登记素材")
            material_id = self._identifier(material_id, "material_id")
            if kind not in MATERIAL_KINDS:
                raise ValidationError("素材种类不在允许范围内")
            title = self._text(title, "title")
            source_description = self._text(source_description, "source_description", 1000)
            territory = self._text(territory, "territory", 100)
            purposes = self._purposes(purposes)
            valid_from_dt = _parse_dt(valid_from, "valid_from")
            valid_until_dt = _parse_dt(valid_until, "valid_until") if valid_until else None
            if valid_until_dt and valid_until_dt <= valid_from_dt:
                raise ValidationError("valid_until 必须晚于 valid_from")
            required_attribution = self._text(required_attribution, "required_attribution", 300)
            holders = self._validate_holders(conn, rights_holder_ids, holder_shares)
            holder_shares = {hid: holders[hid]["share"] for hid in rights_holder_ids}
            coauthor_ids = list(dict.fromkeys(coauthor_ids))
            for coauthor_id in coauthor_ids:
                if coauthor_id not in rights_holder_ids:
                    raise ValidationError(f"联合作者 {coauthor_id} 必须同时在权利人清单中")
                if holders[coauthor_id]["holder"].kind not in {"coauthor", "both"}:
                    raise ValidationError(f"{coauthor_id} 未登记为联合作者身份")
            if alternative_material_id:
                alt = conn.execute("SELECT 1 FROM rmaterials WHERE material_id=? AND work_id=?",
                                   (alternative_material_id, work_id)).fetchone()
                if alt is None:
                    raise ValidationError("替代素材不存在或不属于同一作品")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO rmaterials(material_id,work_id,kind,title,source_description,"
                        "rights_holder_ids_json,holder_shares_json,coauthor_ids_json,territory,"
                        "purposes_json,valid_from,valid_until,required_attribution,"
                        "alternative_material_id,evidence_ids_json,registered_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (material_id, work_id, kind, title, source_description,
                         canonical_json(list(rights_holder_ids)), canonical_json(holder_shares),
                         canonical_json(coauthor_ids),
                         territory, canonical_json(purposes), valid_from, valid_until,
                         required_attribution, alternative_material_id, "[]",
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("素材编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="rights_material.registered",
                             resource_type="rmaterial", resource_id=material_id,
                             detail={"work_id": work_id, "kind": kind, "title": title,
                                     "territory": territory, "purposes": purposes,
                                     "valid_from": valid_from, "valid_until": valid_until,
                                     "rights_holder_ids": list(rights_holder_ids),
                                     "holder_shares": holder_shares,
                                     "coauthor_ids": coauthor_ids,
                                     "alternative_material_id": alternative_material_id},
                             occurred_at=self._now())
                return "rmaterial", material_id, {"material_id": material_id}

            return self._idempotent(conn, request_id=request_id, action="rights_register_material",
                                    payload=payload, create=create)

    def _purposes(self, purposes: list[str]) -> list[str]:
        if not isinstance(purposes, list) or not purposes:
            raise ValidationError("purposes 必须是非空数组")
        cleaned: list[str] = []
        for purpose in purposes:
            purpose = self._text(purpose, "purposes 中的用途", 80)
            cleaned.append(purpose)
        if len(set(cleaned)) != len(cleaned):
            raise ValidationError("purposes 不能重复")
        return cleaned

    def _validate_holders(self, conn, holder_ids: list[str],
                          holder_shares: dict[str, int] | None) -> dict[str, dict[str, Any]]:
        if not isinstance(holder_ids, list) or not holder_ids:
            raise ValidationError("rights_holder_ids 必须是非空数组")
        if len(set(holder_ids)) != len(holder_ids):
            raise ValidationError("权利人清单不能重复")
        holder_shares = holder_shares or {}
        if not isinstance(holder_shares, dict):
            raise ValidationError("holder_shares 必须是 holder_id -> 份额整数 的对象")
        unknown = set(holder_shares) - set(holder_ids)
        if unknown:
            raise ValidationError(f"holder_shares 出现清单外权利人: {sorted(unknown)}")
        result: dict[str, dict[str, Any]] = {}
        total = 0
        for holder_id in holder_ids:
            row = conn.execute("SELECT * FROM rights_holders WHERE holder_id=?",
                               (holder_id,)).fetchone()
            if row is None:
                raise ValidationError(f"权利人 {holder_id} 尚未登记")
            holder = self._holder_from_row(row)
            if holder_id in holder_shares:
                share = int(holder_shares[holder_id])
            else:
                share = holder.share_percent
            if not 0 <= share <= 100:
                raise ValidationError(f"{holder_id} 的份额必须在 0-100 之间")
            result[holder_id] = {"holder": holder, "share": share}
            total += share
        missing = [hid for hid in holder_ids if hid not in holder_shares
                   and result[hid]["holder"].share_percent == 0]
        if missing:
            raise ValidationError(f"以下权利人未约定本素材共同创作份额: {missing}")
        if total != 100:
            raise ValidationError(f"共同创作份额合计必须为 100，当前为 {total}")
        return result

    def get_material(self, material_id: str) -> Material:
        row = self.database.connection.execute(
            "SELECT * FROM rmaterials WHERE material_id=?", (material_id,)).fetchone()
        if row is None:
            raise NotFoundError("素材不存在")
        return self._material_from_row(row)

    def _material_from_row(self, row) -> Material:
        evidence_rows = self.database.connection.execute(
            "SELECT evidence_id FROM revidence WHERE material_id=? ORDER BY created_at, evidence_id",
            (row["material_id"],)).fetchall()
        return Material(
            row["material_id"], row["work_id"], row["kind"], row["title"],
            row["source_description"], tuple(json.loads(row["rights_holder_ids_json"])),
            {hid: int(share) for hid, share in json.loads(row["holder_shares_json"]).items()},
            tuple(json.loads(row["coauthor_ids_json"])), row["territory"],
            tuple(json.loads(row["purposes_json"])), row["valid_from"], row["valid_until"],
            row["required_attribution"], row["alternative_material_id"],
            tuple(r["evidence_id"] for r in evidence_rows), row["registered_by"], row["created_at"],
        )

    # --------------------------------------------------------------- 证据

    def upload_evidence(self, *, request_id: str, actor_id: str, material_id: str,
                        evidence_id: str, filename: str, media_type: str, summary: str,
                        classification: str = "normal",
                        sha256: str | None = None, size_bytes: int | None = None,
                        content_base64: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "material_id": material_id, "evidence_id": evidence_id,
                   "filename": filename, "media_type": media_type, "summary": summary,
                   "classification": classification, "sha256": sha256,
                   "size_bytes": size_bytes, "content_base64": content_base64}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "creator", "licensing_officer")
            material = conn.execute("SELECT * FROM rmaterials WHERE material_id=?",
                                    (material_id,)).fetchone()
            if material is None:
                raise NotFoundError("素材不存在")
            if actor.role == "creator" and material["registered_by"] != actor_id:
                raise PermissionDenied("只能为自己登记的素材上传证明")
            evidence_id = self._identifier(evidence_id, "evidence_id")
            filename = self._text(filename, "filename", 240)
            media_type = self._text(media_type, "media_type", 120)
            summary = self._text(summary, "summary", 2000)
            if classification not in {"normal", "sensitive"}:
                raise ValidationError("classification 必须是 normal/sensitive")
            if content_base64 is not None:
                computed_hash, computed_size = _b64_hash(content_base64)
                if sha256 and sha256.lower() != computed_hash:
                    raise ValidationError("sha256 与文件内容不一致")
                sha256, size_bytes = computed_hash, computed_size
            else:
                if not sha256 or len(sha256) != 64:
                    raise ValidationError("必须提供 64 位 sha256 或 content_base64")
                size_bytes = int(size_bytes or 0)
            sha256 = sha256.lower()

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = conn.execute("SELECT * FROM revidence WHERE sha256=?",
                                        (sha256,)).fetchone()
                if existing:
                    # 相同证据重复上传保持幂等：跨素材引用则明确拒绝，避免哈希复用歧义。
                    if existing["material_id"] != material_id:
                        raise ConflictError("相同内容的证据已登记在其他素材下")
                    return ("revidence", existing["evidence_id"],
                            {"evidence_id": existing["evidence_id"]}, True)
                try:
                    conn.execute(
                        "INSERT INTO revidence(evidence_id,material_id,filename,media_type,sha256,"
                        "size_bytes,summary,classification,uploaded_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (evidence_id, material_id, filename, media_type, sha256, size_bytes,
                         summary, classification, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("证据编号已经存在") from exc
                ids = json.loads(material["evidence_ids_json"])
                if evidence_id not in ids:
                    ids.append(evidence_id)
                    conn.execute("UPDATE rmaterials SET evidence_ids_json=? WHERE material_id=?",
                                 (canonical_json(ids), material_id))
                append_event(conn, actor_id=actor_id, action="rights_evidence.uploaded",
                             resource_type="revidence", resource_id=evidence_id,
                             detail={"material_id": material_id, "sha256": sha256,
                                     "size_bytes": size_bytes, "classification": classification},
                             occurred_at=self._now())
                return "revidence", evidence_id, {"evidence_id": evidence_id}

            return self._idempotent(conn, request_id=request_id, action="rights_upload_evidence",
                                    payload=payload, create=create)

    def get_evidence(self, evidence_id: str, actor_id: str) -> dict[str, Any]:
        """读取证据；敏感证明的摘要对评委类角色脱敏。"""
        row = self.database.connection.execute(
            "SELECT * FROM revidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if row is None:
            raise NotFoundError("证据不存在")
        actor = self._actor(self.database.connection, actor_id)
        data = {
            "evidence_id": row["evidence_id"], "material_id": row["material_id"],
            "filename": row["filename"], "media_type": row["media_type"],
            "sha256": row["sha256"], "size_bytes": row["size_bytes"],
            "summary": row["summary"], "classification": row["classification"],
            "uploaded_by": row["uploaded_by"], "created_at": row["created_at"],
        }
        if row["classification"] == "sensitive" and actor.role in JUDGING_ROLES:
            data["filename"] = "[敏感证明文件名已隐藏]"
            data["media_type"] = "redacted"
            data["summary"] = "[敏感证明内容已隐藏，如需核对请联系授权专员]"
            data["size_bytes"] = None
            data["redacted"] = True
        else:
            data["redacted"] = False
        return data

    def list_evidence(self, material_id: str, actor_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT evidence_id FROM revidence WHERE material_id=? ORDER BY created_at, evidence_id",
            (material_id,)).fetchall()
        return [self.get_evidence(row["evidence_id"], actor_id) for row in rows]

    # ----------------------------------------------------------- 核验队列

    def create_verification_task(self, *, request_id: str, actor_id: str, material_id: str,
                                 task_id: str, notes: str = "") -> Any:
        payload = {"actor_id": actor_id, "material_id": material_id, "task_id": task_id,
                   "notes": notes}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "licensing_officer")
            if conn.execute("SELECT 1 FROM rmaterials WHERE material_id=?",
                            (material_id,)).fetchone() is None:
                raise NotFoundError("素材不存在")
            task_id = self._identifier(task_id, "task_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO rverification_tasks(task_id,material_id,status,verifier_id,"
                        "reviewer_id,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (task_id, material_id, "pending", None, None, notes,
                         self._now(), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("核验任务编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="rights_verification.queued",
                             resource_type="rverification_task", resource_id=task_id,
                             detail={"material_id": material_id}, occurred_at=self._now())
                return "rverification_task", task_id, {"task_id": task_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="rights_create_verification", payload=payload,
                                    create=create)

    def list_pending_tasks(self) -> list[VerificationTask]:
        """待核验队列完全由 SQLite 中的 pending 行构成，重启后保持一致。"""
        rows = self.database.connection.execute(
            "SELECT * FROM rverification_tasks WHERE status='pending' "
            "ORDER BY created_at, task_id").fetchall()
        return [self._task_from_row(row) for row in rows]

    @staticmethod
    def _task_from_row(row) -> VerificationTask:
        return VerificationTask(row["task_id"], row["material_id"], row["status"],
                                row["verifier_id"], row["reviewer_id"], row["created_at"],
                                row["updated_at"], row["notes"])

    def submit_verification(self, *, request_id: str, actor_id: str, task_id: str,
                            decision: str, notes: str = "") -> Any:
        """授权专员完成真实性核验，并据此出具新的权利结论。"""
        payload = {"actor_id": actor_id, "task_id": task_id, "decision": decision, "notes": notes}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "licensing_officer")
            task = conn.execute("SELECT * FROM rverification_tasks WHERE task_id=?",
                                (task_id,)).fetchone()
            if task is None:
                raise NotFoundError("核验任务不存在")
            if task["status"] != "pending":
                raise ConflictError("核验任务已处理，不能重复提交")
            if decision not in {"authentic", "inauthentic"}:
                raise ValidationError("decision 必须是 authentic/inauthentic")
            material = conn.execute("SELECT * FROM rmaterials WHERE material_id=?",
                                    (task["material_id"],)).fetchone()

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE rverification_tasks SET status=?,verifier_id=?,notes=?,updated_at=? "
                    "WHERE task_id=?",
                    (decision, actor_id, notes, self._now(), task_id),
                )
                append_event(conn, actor_id=actor_id,
                             action="rights_verification.submitted",
                             resource_type="rverification_task", resource_id=task_id,
                             detail={"material_id": material["material_id"], "decision": decision},
                             occurred_at=self._now())
                conclusion_id = self._issue_conclusion(
                    conn, material_row=material,
                    decision="cleared" if decision == "authentic" else "blocked",
                    trigger="verification", actor_id=actor_id,
                    basis_extra={"verification_task_id": task_id,
                                 "verification_decision": decision, "verifier_notes": notes})
                return "rverification_task", task_id, {"task_id": task_id,
                                                       "conclusion_id": conclusion_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="rights_submit_verification", payload=payload,
                                    create=create)

    def request_conflict_review(self, *, request_id: str, actor_id: str, task_id: str,
                                reason: str) -> Any:
        """对核验结果有异议时，交由冲突复核员独立复核（须与核验人不同）。"""
        payload = {"actor_id": actor_id, "task_id": task_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "licensing_officer", "creator", "operator")
            task = conn.execute("SELECT * FROM rverification_tasks WHERE task_id=?",
                                (task_id,)).fetchone()
            if task is None:
                raise NotFoundError("核验任务不存在")
            if task["status"] not in {"authentic", "inauthentic"}:
                raise ConflictError("只有已完成真实性核验的任务才能申请冲突复核")
            reason = self._text(reason, "reason", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE rverification_tasks SET notes=?,updated_at=? WHERE task_id=?",
                    (task["notes"] + f"\n[复核请求 by {actor_id}] {reason}", self._now(), task_id),
                )
                append_event(conn, actor_id=actor_id, action="rights_conflict.review_requested",
                             resource_type="rverification_task", resource_id=task_id,
                             detail={"material_id": task["material_id"], "reason": reason,
                                     "verifier_id": task["verifier_id"]},
                             occurred_at=self._now())
                return "rverification_task", task_id, {"task_id": task_id,
                                                       "verifier_id": task["verifier_id"]}

            return self._idempotent(conn, request_id=request_id,
                                    action="rights_request_conflict_review", payload=payload,
                                    create=create)

    def resolve_conflict_review(self, *, request_id: str, actor_id: str, task_id: str,
                                upheld: bool, notes: str) -> Any:
        """冲突复核员裁定：维持则沿用原结论，推翻则出具新结论。复核人不得就是核验人。"""
        payload = {"actor_id": actor_id, "task_id": task_id, "upheld": upheld, "notes": notes}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "conflict_reviewer")
            task = conn.execute("SELECT * FROM rverification_tasks WHERE task_id=?",
                                (task_id,)).fetchone()
            if task is None:
                raise NotFoundError("核验任务不存在")
            if task["reviewer_id"] is not None:
                raise ConflictError("该任务已经完成冲突复核")
            if task["status"] not in {"authentic", "inauthentic"}:
                raise ConflictError("任务尚未完成真实性核验")
            if task["verifier_id"] == actor_id:
                raise PermissionDenied("冲突复核员不能复核本人完成的真实性核验")
            material = conn.execute("SELECT * FROM rmaterials WHERE material_id=?",
                                    (task["material_id"],)).fetchone()

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE rverification_tasks SET reviewer_id=?,notes=?,updated_at=? "
                    "WHERE task_id=?",
                    (actor_id, task["notes"] + f"\n[冲突复核 by {actor_id}] {notes}",
                     self._now(), task_id),
                )
                result: dict[str, Any] = {"task_id": task_id}
                if not upheld:
                    new_decision = "blocked" if task["status"] == "authentic" else "cleared"
                    conclusion_id = self._issue_conclusion(
                        conn, material_row=material, decision=new_decision,
                        trigger="conflict_resolution", actor_id=actor_id,
                        basis_extra={"verification_task_id": task_id,
                                     "original_verification": task["status"],
                                     "review_overturned": True, "review_notes": notes})
                    result["conclusion_id"] = conclusion_id
                append_event(conn, actor_id=actor_id, action="rights_conflict.resolved",
                             resource_type="rverification_task", resource_id=task_id,
                             detail={"material_id": task["material_id"], "upheld": upheld},
                             occurred_at=self._now())
                return "rverification_task", task_id, result

            return self._idempotent(conn, request_id=request_id,
                                    action="rights_resolve_conflict_review", payload=payload,
                                    create=create)

    # ------------------------------------------------------------- 结论链

    def _latest_conclusion_row(self, conn, material_id: str):
        return conn.execute(
            "SELECT * FROM rconclusions WHERE material_id=? ORDER BY sequence_no DESC LIMIT 1",
            (material_id,)).fetchone()

    def _license_snapshot(self, material_row) -> dict[str, Any]:
        shares = {hid: int(share) for hid, share in
                  json.loads(material_row["holder_shares_json"]).items()}
        holder_rows = []
        for holder_id in json.loads(material_row["rights_holder_ids_json"]):
            row = self.database.connection.execute(
                "SELECT holder_id,display_name,kind,can_accept_channel_deals "
                "FROM rights_holders WHERE holder_id=?", (holder_id,)).fetchone()
            holder_rows.append({"holder_id": row["holder_id"], "display_name": row["display_name"],
                                "kind": row["kind"], "share_percent": shares.get(holder_id, 0),
                                "can_accept_channel_deals": bool(row["can_accept_channel_deals"])})
        return {
            "territory": material_row["territory"],
            "purposes": json.loads(material_row["purposes_json"]),
            "valid_from": material_row["valid_from"],
            "valid_until": material_row["valid_until"],
            "required_attribution": material_row["required_attribution"],
            "holders": holder_rows,
            "coauthor_ids": json.loads(material_row["coauthor_ids_json"]),
            "alternative_material_id": material_row["alternative_material_id"],
        }

    def _issue_conclusion(self, conn, *, material_row, decision: str, trigger: str,
                          actor_id: str, basis_extra: dict[str, Any],
                          supersedes_id: str | None = None) -> str:
        if decision not in DECISIONS:
            raise ValidationError("decision 不在允许范围内")
        material_id = material_row["material_id"]
        latest = self._latest_conclusion_row(conn, material_id)
        sequence_no = (latest["sequence_no"] + 1) if latest else 1
        if supersedes_id is None and latest is not None:
            supersedes_id = latest["conclusion_id"]
        evidence_rows = conn.execute(
            "SELECT evidence_id,sha256,classification FROM revidence WHERE material_id=? "
            "ORDER BY created_at, evidence_id", (material_id,)).fetchall()
        basis = {
            "material_id": material_id,
            "license": self._license_snapshot(material_row),
            "evidence": [{"evidence_id": r["evidence_id"], "sha256": r["sha256"],
                          "classification": r["classification"]} for r in evidence_rows],
            "prior_conclusion_id": latest["conclusion_id"] if latest else None,
            "issued_at": self._now(),
            **basis_extra,
        }
        basis_hash = digest(basis)
        conclusion_id = uuid.uuid4().hex
        usable = decision in USABLE_DECISIONS
        conn.execute(
            "INSERT INTO rconclusions(conclusion_id,material_id,sequence_no,decision,trigger,"
            "basis_json,basis_hash,supersedes_id,issued_by,issued_at,usable) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (conclusion_id, material_id, sequence_no, decision, trigger,
             canonical_json(basis), basis_hash, supersedes_id, actor_id,
             basis["issued_at"], 1 if usable else 0),
        )
        append_event(conn, actor_id=actor_id, action="rights_conclusion.issued",
                     resource_type="rconclusion", resource_id=conclusion_id,
                     detail={"material_id": material_id, "sequence_no": sequence_no,
                             "decision": decision, "trigger": trigger,
                             "basis_hash": basis_hash, "supersedes_id": supersedes_id,
                             "usable": usable}, occurred_at=self._now())
        return conclusion_id

    @staticmethod
    def _conclusion_from_row(row) -> RightsConclusion:
        return RightsConclusion(
            row["conclusion_id"], row["material_id"], row["sequence_no"], row["decision"],
            row["trigger"], json.loads(row["basis_json"]), row["basis_hash"],
            row["supersedes_id"], row["issued_by"], row["issued_at"], bool(row["usable"]),
        )

    def list_conclusions(self, material_id: str) -> list[RightsConclusion]:
        if self.database.connection.execute(
                "SELECT 1 FROM rmaterials WHERE material_id=?", (material_id,)).fetchone() is None:
            raise NotFoundError("素材不存在")
        rows = self.database.connection.execute(
            "SELECT * FROM rconclusions WHERE material_id=? ORDER BY sequence_no",
            (material_id,)).fetchall()
        return [self._conclusion_from_row(row) for row in rows]

    def supplement_authorization(self, *, request_id: str, actor_id: str, material_id: str,
                                 decision: str = "cleared", note: str = "") -> Any:
        """评审后补交授权：只追加新结论，不触碰已冻结版本的历史依据。"""
        payload = {"actor_id": actor_id, "material_id": material_id, "decision": decision,
                   "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "licensing_officer")
            if decision not in {"cleared", "restricted"}:
                raise ValidationError("补交授权只能形成 cleared/restricted 结论")
            material = conn.execute("SELECT * FROM rmaterials WHERE material_id=?",
                                    (material_id,)).fetchone()
            if material is None:
                raise NotFoundError("素材不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                conclusion_id = self._issue_conclusion(
                    conn, material_row=material, decision=decision, trigger="supplement",
                    actor_id=actor_id, basis_extra={"supplement_note": note})
                return "rconclusion", conclusion_id, {"conclusion_id": conclusion_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="rights_supplement_authorization", payload=payload,
                                    create=create)

    def revoke_authorization(self, *, request_id: str, actor_id: str, material_id: str,
                             reason: str) -> Any:
        """授权方撤销许可：生成 revoked 结论。"""
        payload = {"actor_id": actor_id, "material_id": material_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "licensing_officer")
            material = conn.execute("SELECT * FROM rmaterials WHERE material_id=?",
                                    (material_id,)).fetchone()
            if material is None:
                raise NotFoundError("素材不存在")
            latest = self._latest_conclusion_row(conn, material_id)
            if latest is None:
                raise ConflictError("素材尚未形成过结论，无需撤销")

            def create() -> tuple[str, str, dict[str, Any]]:
                conclusion_id = self._issue_conclusion(
                    conn, material_row=material, decision="revoked", trigger="revocation",
                    actor_id=actor_id, basis_extra={"revocation_reason": reason})
                return "rconclusion", conclusion_id, {"conclusion_id": conclusion_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="rights_revoke_authorization", payload=payload,
                                    create=create)

    def withdraw_authorization(self, *, request_id: str, actor_id: str, material_id: str,
                               reason: str) -> Any:
        """投稿人撤回授权材料：生成 withdrawn 结论，历史评审依据仍然保留。"""
        payload = {"actor_id": actor_id, "material_id": material_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "licensing_officer", "creator")
            material = conn.execute("SELECT * FROM rmaterials WHERE material_id=?",
                                    (material_id,)).fetchone()
            if material is None:
                raise NotFoundError("素材不存在")
            if actor.role == "creator" and material["registered_by"] != actor_id:
                raise PermissionDenied("只能撤回本人登记素材的授权")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE rverification_tasks SET status='withdrawn',updated_at=? "
                    "WHERE material_id=? AND status='pending'", (self._now(), material_id))
                conclusion_id = self._issue_conclusion(
                    conn, material_row=material, decision="withdrawn", trigger="withdrawal",
                    actor_id=actor_id, basis_extra={"withdrawal_reason": reason})
                return "rconclusion", conclusion_id, {"conclusion_id": conclusion_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="rights_withdraw_authorization", payload=payload,
                                    create=create)

    def sweep_expired_authorizations(self, *, actor_id: str,
                                     at: str | None = None) -> dict[str, Any]:
        """把已超过商业化期限且当前仍可用的素材结论置为 expired，并给出影响面。"""
        now = _parse_dt(at, "at") if at else self.clock.now()
        expired: list[dict[str, Any]] = []
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "auditor", "licensing_officer")
            rows = conn.execute("SELECT * FROM rmaterials WHERE valid_until IS NOT NULL").fetchall()
            for material in rows:
                until = _parse_dt(material["valid_until"], "valid_until")
                if until > now:
                    continue
                latest = self._latest_conclusion_row(conn, material["material_id"])
                if latest is None or not latest["usable"]:
                    continue
                conclusion_id = self._issue_conclusion(
                    conn, material_row=material, decision="expired", trigger="expiry",
                    actor_id=actor_id,
                    basis_extra={"expired_at": material["valid_until"], "swept_at": self._now()})
                impact = self._impact_report(conn, material["material_id"])
                expired.append({"material_id": material["material_id"],
                                "conclusion_id": conclusion_id, "impact": impact})
        return {"expired": expired, "count": len(expired), "swept_at": self._now()}

    def trace_conclusion(self, *, actor_id: str, conclusion_id: str | None = None,
                         material_id: str | None = None) -> dict[str, Any]:
        """授权专员/审计通过 API 追溯一个可用（或任意）结论的完整依据。"""
        conn = self.database.connection
        actor = self._actor(conn, actor_id)
        if actor.role not in CLEARANCE_ROLES:
            raise PermissionDenied("仅授权专员、冲突复核员与审计可追溯完整结论依据")
        if conclusion_id:
            target = conn.execute("SELECT * FROM rconclusions WHERE conclusion_id=?",
                                  (conclusion_id,)).fetchone()
            if target is None:
                raise NotFoundError("结论不存在")
            material_id = target["material_id"]
        if not material_id:
            raise ValidationError("必须提供 conclusion_id 或 material_id")
        material = conn.execute("SELECT * FROM rmaterials WHERE material_id=?",
                                (material_id,)).fetchone()
        if material is None:
            raise NotFoundError("素材不存在")
        chain_rows = conn.execute(
            "SELECT * FROM rconclusions WHERE material_id=? ORDER BY sequence_no",
            (material_id,)).fetchall()
        chain = [self._conclusion_public(conn, row) for row in chain_rows]
        evidence = []
        for row in conn.execute("SELECT * FROM revidence WHERE material_id=? ORDER BY created_at",
                                (material_id,)).fetchall():
            evidence.append({"evidence_id": row["evidence_id"], "filename": row["filename"],
                             "media_type": row["media_type"], "sha256": row["sha256"],
                             "size_bytes": row["size_bytes"], "summary": row["summary"],
                             "classification": row["classification"]})
        tasks = [self._task_from_row(r).__dict__ for r in conn.execute(
            "SELECT * FROM rverification_tasks WHERE material_id=? ORDER BY created_at",
            (material_id,)).fetchall()]
        versions = []
        for row in conn.execute("SELECT * FROM rwork_versions WHERE locked=1").fetchall():
            snapshot = json.loads(row["snapshot_json"] or "{}")
            if material_id in snapshot:
                versions.append({"version_id": row["version_id"], "work_id": row["work_id"],
                                 "stage": row["review_stage"], "locked_at": row["locked_at"],
                                 "pinned": snapshot[material_id]})
        return {
            "material": self._material_from_row(material).__dict__,
            "current_conclusion": chain[-1] if chain else None,
            "chain": chain,
            "evidence": evidence,
            "verification_tasks": tasks,
            "locked_versions_relying": versions,
        }

    def _conclusion_public(self, conn, row) -> dict[str, Any]:
        return {"conclusion_id": row["conclusion_id"], "material_id": row["material_id"],
                "sequence_no": row["sequence_no"], "decision": row["decision"],
                "trigger": row["trigger"], "basis": json.loads(row["basis_json"]),
                "basis_hash": row["basis_hash"], "supersedes_id": row["supersedes_id"],
                "issued_by": row["issued_by"], "issued_at": row["issued_at"],
                "usable": bool(row["usable"])}

    # ------------------------------------------------------- 版本与评审冻结

    def create_work_version(self, *, request_id: str, actor_id: str, work_id: str,
                            version_id: str, material_ids: list[str]) -> Any:
        payload = {"actor_id": actor_id, "work_id": work_id, "version_id": version_id,
                   "material_ids": list(material_ids)}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "creator", "licensing_officer")
            work = conn.execute("SELECT * FROM rworks WHERE work_id=?", (work_id,)).fetchone()
            if work is None:
                raise NotFoundError("作品不存在")
            if actor.role == "creator" and work["primary_creator_id"] != actor_id:
                raise PermissionDenied("只能为自己的作品创建版本")
            version_id = self._identifier(version_id, "version_id")
            if not isinstance(material_ids, list) or not material_ids:
                raise ValidationError("material_ids 必须是非空数组")
            for mid in material_ids:
                row = conn.execute("SELECT material_id FROM rmaterials WHERE material_id=? AND work_id=?",
                                   (mid, work_id)).fetchone()
                if row is None:
                    raise ValidationError(f"素材 {mid} 不属于该作品")

            def create() -> tuple[str, str, dict[str, Any]]:
                max_no = conn.execute("SELECT COALESCE(MAX(version_no),0) AS m FROM rwork_versions "
                                      "WHERE work_id=?", (work_id,)).fetchone()["m"]
                version_no = max_no + 1
                try:
                    conn.execute(
                        "INSERT INTO rwork_versions(version_id,work_id,version_no,submitted_at,"
                        "review_stage,locked,locked_at,material_ids_json,snapshot_json) "
                        "VALUES(?,?,?,?,?,0,NULL,?,NULL)",
                        (version_id, work_id, version_no, self._now(), None,
                         canonical_json(list(material_ids))),
                    )
                except Exception as exc:
                    raise ConflictError("版本编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="rights_version.created",
                             resource_type="rwork_version", resource_id=version_id,
                             detail={"work_id": work_id, "version_no": version_no,
                                     "material_ids": list(material_ids)},
                             occurred_at=self._now())
                return "rwork_version", version_id, {"version_id": version_id,
                                                      "version_no": version_no}

            return self._idempotent(conn, request_id=request_id,
                                    action="rights_create_version", payload=payload, create=create)

    def enter_review(self, *, request_id: str, actor_id: str, work_id: str, stage: str,
                     version_id: str) -> Any:
        """版本进入评审：固化每份素材当时结论的编号与依据哈希；事后不可改写。"""
        payload = {"actor_id": actor_id, "work_id": work_id, "stage": stage,
                   "version_id": version_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "licensing_officer")
            work = conn.execute("SELECT * FROM rworks WHERE work_id=?", (work_id,)).fetchone()
            if work is None:
                raise NotFoundError("作品不存在")
            version = conn.execute("SELECT * FROM rwork_versions WHERE version_id=? AND work_id=?",
                                   (version_id, work_id)).fetchone()
            if version is None:
                raise NotFoundError("作品版本不存在")
            if version["locked"]:
                raise ConflictError("该版本已进入评审，依据已冻结")
            stage = self._text(stage, "stage", 60)
            if conn.execute("SELECT 1 FROM rreviews WHERE work_id=? AND stage=?",
                            (work_id, stage)).fetchone():
                raise ConflictError("该评审阶段已有进入记录")
            material_ids = json.loads(version["material_ids_json"])
            snapshot: dict[str, dict[str, str]] = {}
            for mid in material_ids:
                latest = self._latest_conclusion_row(conn, mid)
                if latest is None:
                    raise ConflictError(f"素材 {mid} 尚未形成权利结论，不能进入评审")
                snapshot[mid] = {"conclusion_id": latest["conclusion_id"],
                                 "decision": latest["decision"],
                                 "basis_hash": latest["basis_hash"]}
            for mid in material_ids:
                freeze = conn.execute(
                    "SELECT 1 FROM rfreezes WHERE lifted=0 AND version_id=? "
                    "AND (material_id IS NULL OR material_id=?)",
                    (version_id, mid)).fetchone()
                if freeze:
                    raise ConflictError(f"版本因异议被局部冻结（素材 {mid}），不能进入评审")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE rwork_versions SET locked=1,locked_at=?,review_stage=?,snapshot_json=? "
                    "WHERE version_id=?",
                    (self._now(), stage, canonical_json(snapshot), version_id),
                )
                conn.execute(
                    "INSERT INTO rreviews(work_id,stage,version_id,entered_at) VALUES(?,?,?,?)",
                    (work_id, stage, version_id, self._now()),
                )
                conn.execute("UPDATE rworks SET current_stage=? WHERE work_id=?", (stage, work_id))
                append_event(conn, actor_id=actor_id, action="rights_review.entered",
                             resource_type="rwork", resource_id=work_id,
                             detail={"stage": stage, "version_id": version_id,
                                     "snapshot": snapshot}, occurred_at=self._now())
                return "rreview", f"{work_id}:{stage}", {"version_id": version_id, "stage": stage}

            return self._idempotent(conn, request_id=request_id,
                                    action="rights_enter_review", payload=payload, create=create)

    def get_version(self, version_id: str) -> WorkVersion:
        row = self.database.connection.execute(
            "SELECT * FROM rwork_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("版本不存在")
        return WorkVersion(
            row["version_id"], row["work_id"], row["version_no"], row["submitted_at"],
            row["review_stage"], bool(row["locked"]), row["locked_at"],
            tuple(json.loads(row["material_ids_json"])),
            json.loads(row["snapshot_json"]) if row["snapshot_json"] else {},
        )

    def list_reviews(self, work_id: str) -> list[ReviewEntry]:
        rows = self.database.connection.execute(
            "SELECT * FROM rreviews WHERE work_id=? ORDER BY entered_at", (work_id,)).fetchall()
        return [ReviewEntry(r["work_id"], r["stage"], r["version_id"], r["entered_at"])
                for r in rows]

    def review_pack(self, *, actor_id: str, work_id: str) -> dict[str, Any]:
        """评委评审包：包含署名/权利人/结论状态，敏感证明自动脱敏。"""
        conn = self.database.connection
        actor = self._actor(conn, actor_id)
        work = conn.execute("SELECT * FROM rworks WHERE work_id=?", (work_id,)).fetchone()
        if work is None:
            raise NotFoundError("作品不存在")
        versions = []
        for vrow in conn.execute(
                "SELECT * FROM rwork_versions WHERE work_id=? ORDER BY version_no",
                (work_id,)).fetchall():
            snapshot = json.loads(vrow["snapshot_json"] or "{}")
            versions.append({"version_id": vrow["version_id"], "version_no": vrow["version_no"],
                             "review_stage": vrow["review_stage"], "locked": bool(vrow["locked"]),
                             "locked_at": vrow["locked_at"],
                             "material_ids": json.loads(vrow["material_ids_json"]),
                             "pinned_basis": snapshot})
        materials = []
        for mrow in conn.execute(
                "SELECT * FROM rmaterials WHERE work_id=? ORDER BY created_at, material_id",
                (work_id,)).fetchall():
            latest = self._latest_conclusion_row(conn, mrow["material_id"])
            holders = [self._holder_from_row(conn.execute(
                "SELECT * FROM rights_holders WHERE holder_id=?", (hid,)).fetchone()).__dict__
                for hid in json.loads(mrow["rights_holder_ids_json"])]
            ev_items = []
            for erow in conn.execute(
                    "SELECT * FROM revidence WHERE material_id=? ORDER BY created_at, evidence_id",
                    (mrow["material_id"],)).fetchall():
                item = {"evidence_id": erow["evidence_id"], "sha256": erow["sha256"],
                        "classification": erow["classification"]}
                if erow["classification"] == "sensitive" and actor.role in JUDGING_ROLES:
                    item["summary"] = "[敏感证明内容已隐藏]"
                    item["filename"] = "[敏感证明文件名已隐藏]"
                else:
                    item["summary"] = erow["summary"]
                    item["filename"] = erow["filename"]
                ev_items.append(item)
            materials.append({
                "material": self._material_from_row(mrow).__dict__,
                "holders": holders,
                "current_decision": latest["decision"] if latest else None,
                "current_conclusion_id": latest["conclusion_id"] if latest else None,
                "evidence": ev_items,
            })
        return {"work": self.get_work(work_id).__dict__, "current_stage": work["current_stage"],
                "versions": versions, "materials": materials,
                "viewer_role": actor.role}

    # ------------------------------------------------------------- 影响分析

    def _territory_match(self, material_territory: str, deal_territory: str) -> bool:
        return material_territory in {"*", "CN-ALL"} or deal_territory in {"*", "CN-ALL"} \
            or material_territory == deal_territory

    def _impact_report(self, conn, material_id: str) -> dict[str, Any]:
        material = conn.execute("SELECT * FROM rmaterials WHERE material_id=?",
                                (material_id,)).fetchone()
        purposes = set(json.loads(material["purposes_json"]))
        affected_versions = []
        affected_work_ids: set[str] = set()
        for vrow in conn.execute(
                "SELECT * FROM rwork_versions WHERE locked=1 ORDER BY locked_at").fetchall():
            snapshot = json.loads(vrow["snapshot_json"] or "{}")
            pinned = snapshot.get(material_id)
            if not pinned:
                continue
            affected_versions.append({
                "work_id": vrow["work_id"], "version_id": vrow["version_id"],
                "stage": vrow["review_stage"], "locked_at": vrow["locked_at"],
                "pinned_conclusion_id": pinned["conclusion_id"],
                "pinned_decision": pinned["decision"],
                "pinned_basis_hash": pinned["basis_hash"],
            })
            affected_work_ids.add(vrow["work_id"])
        unsigned_deals = []
        for work_id in affected_work_ids:
            for drow in conn.execute(
                    "SELECT * FROM rdeals WHERE work_id=? AND status='proposed'",
                    (work_id,)).fetchall():
                deal_purposes = set(json.loads(drow["requires_purposes_json"]))
                if (deal_purposes & purposes
                        and self._territory_match(material["territory"], drow["territory"])):
                    unsigned_deals.append({"deal_id": drow["deal_id"], "work_id": work_id,
                                           "partner": drow["partner"],
                                           "territory": drow["territory"],
                                           "requires_purposes": list(deal_purposes)})
        return {"material_id": material_id, "affected_versions": affected_versions,
                "unsigned_deals": unsigned_deals}

    def impact_report(self, *, actor_id: str, material_id: str) -> dict[str, Any]:
        actor = self._actor(self.database.connection, actor_id)
        if actor.role not in CLEARANCE_ROLES | {"operator", "creator"}:
            raise PermissionDenied("当前角色不能查看权利影响分析")
        return self._impact_report(self.database.connection, material_id)

    # ------------------------------------------------------------- 商业合作

    def register_deal(self, *, request_id: str, actor_id: str, deal_id: str, work_id: str,
                      partner: str, requires_purposes: list[str], territory: str) -> Any:
        payload = {"actor_id": actor_id, "deal_id": deal_id, "work_id": work_id,
                   "partner": partner, "requires_purposes": list(requires_purposes),
                   "territory": territory}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "creator", "licensing_officer")
            if conn.execute("SELECT 1 FROM rworks WHERE work_id=?", (work_id,)).fetchone() is None:
                raise NotFoundError("作品不存在")
            deal_id = self._identifier(deal_id, "deal_id")
            partner = self._text(partner, "partner")
            requires_purposes = self._purposes(requires_purposes)
            territory = self._text(territory, "territory", 100)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO rdeals(deal_id,work_id,partner,status,requires_purposes_json,"
                        "territory,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (deal_id, work_id, partner, "proposed",
                         canonical_json(requires_purposes), territory, self._now(), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("商业合作编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="rights_deal.registered",
                             resource_type="rdeal", resource_id=deal_id,
                             detail={"work_id": work_id, "partner": partner,
                                     "territory": territory,
                                     "requires_purposes": requires_purposes},
                             occurred_at=self._now())
                return "rdeal", deal_id, {"deal_id": deal_id}

            return self._idempotent(conn, request_id=request_id, action="rights_register_deal",
                                    payload=payload, create=create)

    def _channel_representatives(self, conn, work_id: str) -> list[str]:
        representative_ids: set[str] = set()
        rows = conn.execute(
            "SELECT rights_holder_ids_json FROM rmaterials WHERE work_id=?", (work_id,)).fetchall()
        for row in rows:
            for holder_id in json.loads(row["rights_holder_ids_json"]):
                holder = conn.execute(
                    "SELECT can_accept_channel_deals FROM rights_holders WHERE holder_id=?",
                    (holder_id,)).fetchone()
                if holder and holder["can_accept_channel_deals"]:
                    representative_ids.add(holder_id)
        return sorted(representative_ids)

    def sign_deal(self, *, request_id: str, actor_id: str, deal_id: str) -> Any:
        """签署渠道合作：存在局部冻结或无获授权的团队代表时拒绝。"""
        payload = {"actor_id": actor_id, "deal_id": deal_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "licensing_officer")
            deal = conn.execute("SELECT * FROM rdeals WHERE deal_id=?", (deal_id,)).fetchone()
            if deal is None:
                raise NotFoundError("商业合作不存在")
            if deal["status"] != "proposed":
                raise ConflictError("该合作不在待签署状态")
            freeze = conn.execute(
                "SELECT 1 FROM rfreezes f JOIN rwork_versions v ON f.version_id=v.version_id "
                "WHERE f.lifted=0 AND v.work_id=?", (deal["work_id"],)).fetchone()
            if freeze:
                raise ConflictError("作品存在未解除的局部冻结，不能签署合作")
            deal_purposes = set(json.loads(deal["requires_purposes_json"]))
            blocking: list[str] = []
            for material in conn.execute(
                    "SELECT * FROM rmaterials WHERE work_id=?", (deal["work_id"],)).fetchall():
                material_purposes = set(json.loads(material["purposes_json"]))
                if not (deal_purposes & material_purposes):
                    continue
                if not self._territory_match(material["territory"], deal["territory"]):
                    continue
                latest = self._latest_conclusion_row(conn, material["material_id"])
                if latest is None or not latest["usable"]:
                    decision = latest["decision"] if latest else "unverified"
                    blocking.append(f"{material['material_id']}({decision})")
            if blocking:
                raise ConflictError(f"以下素材当前无可用授权，不能签署合作: {blocking}")
            representatives = self._channel_representatives(conn, deal["work_id"])
            if not representatives:
                raise PermissionDenied("没有获授权代表团队接受渠道合作的权利人，需先解决联合作者争议")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE rdeals SET status='signed',updated_at=? WHERE deal_id=?",
                    (self._now(), deal_id))
                append_event(conn, actor_id=actor_id, action="rights_deal.signed",
                             resource_type="rdeal", resource_id=deal_id,
                             detail={"work_id": deal["work_id"],
                                     "accepted_by_holders": representatives},
                             occurred_at=self._now())
                return "rdeal", deal_id, {"deal_id": deal_id,
                                          "accepted_by_holders": representatives}

            return self._idempotent(conn, request_id=request_id, action="rights_sign_deal",
                                    payload=payload, create=create)

    def block_deal(self, *, request_id: str, actor_id: str, deal_id: str, reason: str) -> Any:
        payload = {"actor_id": actor_id, "deal_id": deal_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "licensing_officer", "conflict_reviewer")
            deal = conn.execute("SELECT * FROM rdeals WHERE deal_id=?", (deal_id,)).fetchone()
            if deal is None:
                raise NotFoundError("商业合作不存在")
            if deal["status"] != "proposed":
                raise ConflictError("只能阻断尚未签署的合作")
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE rdeals SET status='blocked',updated_at=? WHERE deal_id=?",
                             (self._now(), deal_id))
                append_event(conn, actor_id=actor_id, action="rights_deal.blocked",
                             resource_type="rdeal", resource_id=deal_id,
                             detail={"reason": reason}, occurred_at=self._now())
                return "rdeal", deal_id, {"deal_id": deal_id}

            return self._idempotent(conn, request_id=request_id, action="rights_block_deal",
                                    payload=payload, create=create)

    def list_deals(self, work_id: str) -> list[CommercialDeal]:
        rows = self.database.connection.execute(
            "SELECT * FROM rdeals WHERE work_id=? ORDER BY created_at, deal_id",
            (work_id,)).fetchall()
        return [CommercialDeal(r["deal_id"], r["work_id"], r["partner"], r["status"],
                               tuple(json.loads(r["requires_purposes_json"])), r["territory"],
                               r["created_at"], r["updated_at"]) for r in rows]

    # ----------------------------------------------------------------- 异议

    def file_dispute(self, *, request_id: str, actor_id: str, dispute_id: str, work_id: str,
                     reason: str, material_id: str | None = None,
                     scope: dict[str, Any] | None = None) -> Any:
        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "work_id": work_id,
                   "reason": reason, "material_id": material_id, "scope": scope or {}}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator", "creator", "licensing_officer",
                          "conflict_reviewer")
            if conn.execute("SELECT 1 FROM rworks WHERE work_id=?", (work_id,)).fetchone() is None:
                raise NotFoundError("作品不存在")
            if material_id and conn.execute(
                    "SELECT 1 FROM rmaterials WHERE material_id=? AND work_id=?",
                    (material_id, work_id)).fetchone() is None:
                raise ValidationError("异议素材不存在或不属于该作品")
            dispute_id = self._identifier(dispute_id, "dispute_id")
            reason = self._text(reason, "reason", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO rdisputes(dispute_id,work_id,material_id,reason,status,"
                        "filed_by,filed_at,scope_json,resolution_json) VALUES(?,?,?,?,?,?,?,?,?)",
                        (dispute_id, work_id, material_id, reason, "filed", actor_id,
                         self._now(), canonical_json(scope or {}), "{}"),
                    )
                except Exception as exc:
                    raise ConflictError("异议编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="rights_dispute.filed",
                             resource_type="rdispute", resource_id=dispute_id,
                             detail={"work_id": work_id, "material_id": material_id},
                             occurred_at=self._now())
                return "rdispute", dispute_id, {"dispute_id": dispute_id}

            return self._idempotent(conn, request_id=request_id, action="rights_file_dispute",
                                    payload=payload, create=create)

    def preserve_evidence(self, *, request_id: str, actor_id: str, dispute_id: str,
                          evidence_ids: list[str]) -> Any:
        """证据保全：把证据当前哈希固定到案件下，之后证据变化也不影响保全记录。"""
        payload = {"actor_id": actor_id, "dispute_id": dispute_id,
                   "evidence_ids": list(evidence_ids)}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "licensing_officer", "conflict_reviewer")
            dispute = conn.execute("SELECT * FROM rdisputes WHERE dispute_id=?",
                                   (dispute_id,)).fetchone()
            if dispute is None:
                raise NotFoundError("异议案件不存在")
            if dispute["status"] in {"settled", "adjudicated", "rejected"}:
                raise ConflictError("案件已结案，不能追加保全")
            items = []
            work_materials = {r["material_id"] for r in conn.execute(
                "SELECT material_id FROM rmaterials WHERE work_id=?",
                (dispute["work_id"],)).fetchall()}
            for evidence_id in dict.fromkeys(evidence_ids):
                ev = conn.execute("SELECT * FROM revidence WHERE evidence_id=?",
                                  (evidence_id,)).fetchone()
                if ev is None:
                    raise NotFoundError(f"证据 {evidence_id} 不存在")
                if ev["material_id"] not in work_materials:
                    raise ValidationError(f"证据 {evidence_id} 与异议作品无关")
                items.append(ev)

            def create() -> tuple[str, str, dict[str, Any]]:
                preserved = []
                for ev in items:
                    preservation_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT OR IGNORE INTO rdispute_preservations(preservation_id,dispute_id,"
                        "evidence_id,sha256,summary,preserved_at) VALUES(?,?,?,?,?,?)",
                        (preservation_id, dispute_id, ev["evidence_id"], ev["sha256"],
                         ev["summary"], self._now()),
                    )
                    preserved.append({"evidence_id": ev["evidence_id"], "sha256": ev["sha256"]})
                conn.execute("UPDATE rdisputes SET status='preserved' WHERE dispute_id=? AND status='filed'",
                             (dispute_id,))
                append_event(conn, actor_id=actor_id, action="rights_dispute.evidence_preserved",
                             resource_type="rdispute", resource_id=dispute_id,
                             detail={"evidence": preserved}, occurred_at=self._now())
                return "rdispute", dispute_id, {"dispute_id": dispute_id, "preserved": preserved}

            return self._idempotent(conn, request_id=request_id,
                                    action="rights_preserve_evidence", payload=payload,
                                    create=create)

    def partial_freeze(self, *, request_id: str, actor_id: str, dispute_id: str,
                       version_id: str, material_id: str | None = None) -> Any:
        """局部冻结：冻结指定版本（可精确到某素材）的评审使用，不影响其他版本/素材。"""
        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "version_id": version_id,
                   "material_id": material_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "conflict_reviewer", "licensing_officer")
            dispute = conn.execute("SELECT * FROM rdisputes WHERE dispute_id=?",
                                   (dispute_id,)).fetchone()
            if dispute is None:
                raise NotFoundError("异议案件不存在")
            if dispute["status"] in {"settled", "adjudicated", "rejected"}:
                raise ConflictError("案件已结案")
            version = conn.execute("SELECT * FROM rwork_versions WHERE version_id=?",
                                   (version_id,)).fetchone()
            if version is None or version["work_id"] != dispute["work_id"]:
                raise ValidationError("版本不存在或不属于异议作品")
            if material_id:
                if conn.execute("SELECT 1 FROM rmaterials WHERE material_id=? AND work_id=?",
                                (material_id, dispute["work_id"])).fetchone() is None:
                    raise ValidationError("素材不存在或不属于异议作品")

            def create() -> tuple[str, str, dict[str, Any]]:
                freeze_id = uuid.uuid4().hex
                try:
                    conn.execute(
                        "INSERT INTO rfreezes(freeze_id,dispute_id,version_id,material_id,lifted,"
                        "created_at) VALUES(?,?,?,?,0,?)",
                        (freeze_id, dispute_id, version_id, material_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("冻结记录创建失败") from exc
                conn.execute("UPDATE rdisputes SET status='partially_frozen' WHERE dispute_id=?",
                             (dispute_id,))
                append_event(conn, actor_id=actor_id, action="rights_dispute.partial_freeze",
                             resource_type="rdispute", resource_id=dispute_id,
                             detail={"freeze_id": freeze_id, "version_id": version_id,
                                     "material_id": material_id}, occurred_at=self._now())
                return "rfreeze", freeze_id, {"freeze_id": freeze_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="rights_partial_freeze", payload=payload, create=create)

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                        outcome: str, terms: str,
                        new_decision: str | None = None) -> Any:
        """和解或裁定：结案并解除冻结；如对权利状态有新认定，追加冲突解决结论。"""
        payload = {"actor_id": actor_id, "dispute_id": dispute_id, "outcome": outcome,
                   "terms": terms, "new_decision": new_decision}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "conflict_reviewer")
            dispute = conn.execute("SELECT * FROM rdisputes WHERE dispute_id=?",
                                   (dispute_id,)).fetchone()
            if dispute is None:
                raise NotFoundError("异议案件不存在")
            if dispute["status"] in {"settled", "adjudicated", "rejected"}:
                raise ConflictError("案件已经结案")
            if outcome not in {"settled", "adjudicated", "rejected"}:
                raise ValidationError("outcome 必须是 settled/adjudicated/rejected")
            terms = self._text(terms, "terms", 2000)
            if new_decision is not None and new_decision not in DECISIONS:
                raise ValidationError("new_decision 不是合法结论")

            def create() -> tuple[str, str, dict[str, Any]]:
                new_conclusion_id = None
                if new_decision is not None and dispute["material_id"]:
                    material = conn.execute(
                        "SELECT * FROM rmaterials WHERE material_id=?",
                        (dispute["material_id"],)).fetchone()
                    new_conclusion_id = self._issue_conclusion(
                        conn, material_row=material, decision=new_decision,
                        trigger="conflict_resolution", actor_id=actor_id,
                        basis_extra={"dispute_id": dispute_id, "outcome": outcome, "terms": terms})
                conn.execute("UPDATE rfreezes SET lifted=1 WHERE dispute_id=? AND lifted=0",
                             (dispute_id,))
                resolution = {"outcome": outcome, "terms": terms,
                              "decided_by": actor_id, "decided_at": self._now(),
                              "new_conclusion_id": new_conclusion_id}
                conn.execute(
                    "UPDATE rdisputes SET status=?,resolution_json=? WHERE dispute_id=?",
                    (outcome, canonical_json(resolution), dispute_id))
                append_event(conn, actor_id=actor_id, action="rights_dispute.resolved",
                             resource_type="rdispute", resource_id=dispute_id,
                             detail={"outcome": outcome, "new_conclusion_id": new_conclusion_id},
                             occurred_at=self._now())
                return "rdispute", dispute_id, {"dispute_id": dispute_id, "outcome": outcome,
                                                "new_conclusion_id": new_conclusion_id}

            return self._idempotent(conn, request_id=request_id,
                                    action="rights_resolve_dispute", payload=payload,
                                    create=create)

    def get_dispute(self, dispute_id: str) -> Dispute:
        row = self.database.connection.execute(
            "SELECT * FROM rdisputes WHERE dispute_id=?", (dispute_id,)).fetchone()
        if row is None:
            raise NotFoundError("异议案件不存在")
        return Dispute(row["dispute_id"], row["work_id"], row["material_id"], row["reason"],
                       row["status"], row["filed_by"], row["filed_at"],
                       json.loads(row["scope_json"]), json.loads(row["resolution_json"]))

    def list_preservations(self, dispute_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM rdispute_preservations WHERE dispute_id=? ORDER BY preserved_at",
            (dispute_id,)).fetchall()
        return [{"preservation_id": r["preservation_id"], "evidence_id": r["evidence_id"],
                 "sha256": r["sha256"], "summary": r["summary"],
                 "preserved_at": r["preserved_at"]} for r in rows]

    def list_freezes(self, dispute_id: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT f.*, v.work_id FROM rfreezes f JOIN rwork_versions v "
                 "ON f.version_id=v.version_id")
        parameters: list[Any] = []
        if dispute_id:
            query += " WHERE f.dispute_id=?"
            parameters.append(dispute_id)
        query += " ORDER BY f.created_at"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [{"freeze_id": r["freeze_id"], "dispute_id": r["dispute_id"],
                 "version_id": r["version_id"], "work_id": r["work_id"],
                 "material_id": r["material_id"], "lifted": bool(r["lifted"]),
                 "created_at": r["created_at"]} for r in rows]
