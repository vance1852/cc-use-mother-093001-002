"""文化素材权利链核验的领域服务。

在基础服务（操作者、场所、幂等回执、哈希链审计）之上实现：
素材登记与双职责核验、进入评审后不可改写的权利结论、许可失效影响分析、
异议立案/证据保全/局部冻结/和解/裁定、敏感证明的分级读取，
以及中断恢复后的队列与证据哈希一致性校验。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from creative_program_foundation.audit import append_event, canonical_json, digest, verify_chain
from creative_program_foundation.clock import Clock, SystemClock
from creative_program_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from creative_program_foundation.models import Actor

from . import domain
from .storage import RightsDatabase


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")


class RightsChainService:
    """协调素材、许可、核验、结论、异议与商业合作的规则。"""

    def __init__(self, database: RightsDatabase, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ---- 基础工具 ----

    def _now(self) -> str:
        return self.clock.now().replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _identifier(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _optional_text(self, value: Any, field: str, limit: int = 500) -> str:
        text = str(value or "").strip()
        if len(text) > limit:
            raise ValidationError(f"{field} 不能超过 {limit} 个字符")
        return text

    def _date(self, value: Any, field: str, allow_none: bool = False) -> str | None:
        if value is None or str(value).strip() == "":
            if allow_none:
                return None
            raise ValidationError(f"{field} 不能为空")
        text = str(value).strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                moment = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            else:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 日期或日期时间") from exc
        if moment.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _optional_actor(self, connection, actor_id: str) -> Actor | None:
        if not actor_id:
            return None
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None or not row["active"]:
            return None
        return Actor(row["actor_id"], row["display_name"], row["role"],
                     row["organization_id"], bool(row["active"]))

    def _require(self, actor: Actor, roles: frozenset, message: str = "当前角色不能执行该动作") -> None:
        if actor.role not in roles:
            raise PermissionDenied(message)

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {**json.loads(row["response_json"]), "replayed": True}
        response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, response["resource_type"], response["resource_id"],
             canonical_json(response), self._now()),
        )
        return {**response, "replayed": False}

    # ---- 行记录读取 ----

    def _material_row(self, connection, material_id: str):
        row = connection.execute("SELECT * FROM rights_materials WHERE material_id=?", (material_id,)).fetchone()
        if row is None:
            raise NotFoundError("素材不存在")
        return row

    def _work_row(self, connection, work_id: str):
        row = connection.execute("SELECT * FROM rights_works WHERE work_id=?", (work_id,)).fetchone()
        if row is None:
            raise NotFoundError("作品不存在")
        return row

    def _version_row(self, connection, version_id: str):
        row = connection.execute("SELECT * FROM rights_work_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("作品版本不存在")
        return row

    def _license_row(self, connection, license_id: str):
        row = connection.execute("SELECT * FROM rights_licenses WHERE license_id=?", (license_id,)).fetchone()
        if row is None:
            raise NotFoundError("许可不存在")
        return row

    def _conclusion_row(self, connection, conclusion_id: str):
        row = connection.execute("SELECT * FROM rights_conclusions WHERE conclusion_id=?", (conclusion_id,)).fetchone()
        if row is None:
            raise NotFoundError("权利结论不存在")
        return row

    def _objection_row(self, connection, objection_id: str):
        row = connection.execute("SELECT * FROM rights_objections WHERE objection_id=?", (objection_id,)).fetchone()
        if row is None:
            raise NotFoundError("异议不存在")
        return row

    def _collaboration_row(self, connection, collaboration_id: str):
        row = connection.execute("SELECT * FROM rights_collaborations WHERE collaboration_id=?", (collaboration_id,)).fetchone()
        if row is None:
            raise NotFoundError("商业合作不存在")
        return row

    def _task_row(self, connection, task_id: str):
        row = connection.execute("SELECT * FROM rights_verification_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("核验任务不存在")
        return row

    def _active_freeze(self, connection, target_type: str, target_id: str):
        return connection.execute(
            "SELECT * FROM rights_freezes WHERE target_type=? AND target_id=? AND status='active'",
            (target_type, target_id),
        ).fetchone()

    def _target_materials(self, connection, target_type: str, target_id: str) -> set[str]:
        """把异议或冻结对象解析为受影响的素材集合。"""

        if target_type == "material":
            self._material_row(connection, target_id)
            return {target_id}
        if target_type == "version":
            self._version_row(connection, target_id)
            rows = connection.execute(
                "SELECT material_id FROM rights_version_materials WHERE version_id=?", (target_id,)
            ).fetchall()
            return {row["material_id"] for row in rows}
        if target_type == "conclusion":
            conclusion = self._conclusion_row(connection, target_id)
            return self._target_materials(connection, "version", conclusion["version_id"])
        if target_type == "work":
            self._work_row(connection, target_id)
            rows = connection.execute(
                "SELECT vm.material_id FROM rights_version_materials vm "
                "JOIN rights_work_versions v ON vm.version_id=v.version_id WHERE v.work_id=?",
                (target_id,),
            ).fetchall()
            return {row["material_id"] for row in rows}
        if target_type == "collaboration":
            collaboration = self._collaboration_row(connection, target_id)
            return self._target_materials(connection, "version", collaboration["version_id"])
        raise ValidationError("未知的对象类型")

    # ---- 输入校验 ----

    def _validate_holders(self, rights_holders: Any) -> list[dict[str, Any]]:
        if not isinstance(rights_holders, (list, tuple)) or not rights_holders:
            raise ValidationError("rights_holders 必须是非空列表")
        holders = []
        total = 0
        names = set()
        for item in rights_holders:
            if not isinstance(item, dict):
                raise ValidationError("权利人条目必须是对象")
            name = self._text(item.get("name", ""), "holder.name", 120)
            if name in names:
                raise ValidationError("权利人姓名重复")
            share = item.get("share_percent")
            if not isinstance(share, int) or isinstance(share, bool) or share <= 0 or share > 100:
                raise ValidationError("share_percent 必须是 1 到 100 的整数")
            names.add(name)
            total += share
            holders.append({"name": name, "share_percent": share})
        if total != 100:
            raise ValidationError("共同创作份额合计必须等于 100")
        return holders

    def _validate_license(self, license_spec: Any) -> dict[str, Any]:
        if not isinstance(license_spec, dict):
            raise ValidationError("license 必须是对象")
        territory = self._text(license_spec.get("territory", ""), "license.territory", 120)
        usage_scope = self._text(license_spec.get("usage_scope", ""), "license.usage_scope", 200)
        required_attribution = self._text(license_spec.get("required_attribution", ""),
                                          "license.required_attribution", 200)
        valid_from = self._date(license_spec.get("valid_from"), "license.valid_from")
        valid_until = self._date(license_spec.get("valid_until"), "license.valid_until", allow_none=True)
        if valid_until and valid_until < valid_from:
            raise ValidationError("许可期限起止颠倒")
        return {"territory": territory, "usage_scope": usage_scope,
                "required_attribution": required_attribution,
                "valid_from": valid_from, "valid_until": valid_until}

    def _validate_alternatives(self, connection, alternatives: Any) -> list[dict[str, Any]]:
        if alternatives is None:
            return []
        if not isinstance(alternatives, (list, tuple)):
            raise ValidationError("alternatives 必须是列表")
        specs = []
        for item in alternatives:
            if not isinstance(item, dict):
                raise ValidationError("替代素材条目必须是对象")
            note = self._text(item.get("note", ""), "alternative.note", 200)
            alternative_id = item.get("material_id")
            if alternative_id:
                alternative_id = self._identifier(alternative_id, "alternative.material_id")
                self._material_row(connection, alternative_id)
            specs.append({"material_id": alternative_id, "note": note})
        return specs

    def _validate_evidence(self, evidence: Any) -> list[dict[str, Any]]:
        if evidence is None:
            return []
        if not isinstance(evidence, (list, tuple)):
            raise ValidationError("evidence 必须是列表")
        specs = []
        for item in evidence:
            if not isinstance(item, dict):
                raise ValidationError("证明条目必须是对象")
            specs.append({
                "content": self._text(item.get("content", ""), "evidence.content", 4000),
                "summary": self._text(item.get("summary", ""), "evidence.summary", 500),
                "sensitive": bool(item.get("sensitive", False)),
            })
        return specs

    def _evidence_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # ---- 登记 ----

    def _insert_license(self, connection, *, material_id: str, spec: dict[str, Any],
                        actor_id: str, now_iso: str) -> str:
        license_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO rights_licenses(license_id,material_id,territory,usage_scope,valid_from,valid_until,"
            "required_attribution,status,submitted_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (license_id, material_id, spec["territory"], spec["usage_scope"], spec["valid_from"],
             spec["valid_until"], spec["required_attribution"], domain.LICENSE_ACTIVE, actor_id, now_iso),
        )
        return license_id

    def _insert_evidence(self, connection, *, material_id: str, spec: dict[str, Any],
                         actor_id: str, now_iso: str) -> tuple[str, str, bool]:
        """按内容哈希幂等插入证明，返回 (evidence_id, content_hash, 是否重复)。"""

        content_hash = self._evidence_hash(spec["content"])
        existing = connection.execute(
            "SELECT evidence_id FROM rights_evidence WHERE material_id=? AND content_hash=?",
            (material_id, content_hash),
        ).fetchone()
        if existing:
            return existing["evidence_id"], content_hash, True
        evidence_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO rights_evidence(evidence_id,material_id,content,content_hash,summary,sensitive,uploaded_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (evidence_id, material_id, spec["content"], content_hash, spec["summary"],
             1 if spec["sensitive"] else 0, actor_id, now_iso),
        )
        return evidence_id, content_hash, False

    def register_material(self, *, request_id: str, actor_id: str, site_id: str, work_id: str,
                          title: str, source_type: str, source_description: str,
                          rights_holders: list[dict[str, Any]], license: dict[str, Any],
                          alternatives: list[dict[str, Any]] | None = None,
                          evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """创作者登记素材来源、权利人、份额、许可、署名、证明摘要与替代素材。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "work_id": work_id, "title": title,
                   "source_type": source_type, "source_description": source_description,
                   "rights_holders": rights_holders, "license": license,
                   "alternatives": list(alternatives or []), "evidence": list(evidence or [])}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, domain.REGISTER_ROLES)
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if actor.organization_id != site["organization_id"] and actor.role != domain.ROLE_ADMIN:
                raise PermissionDenied("不能写入其他组织的场所")
            work = self._work_row(connection, work_id)
            if work["site_id"] != site_id:
                raise ValidationError("作品不属于该场所")
            title = self._text(title, "title")
            source_type = self._text(source_type, "source_type", 40)
            source_description = self._text(source_description, "source_description", 500)
            holders = self._validate_holders(rights_holders)
            license_spec = self._validate_license(license)
            alternative_specs = self._validate_alternatives(connection, alternatives)
            evidence_specs = self._validate_evidence(evidence)

            def create() -> dict[str, Any]:
                material_id = uuid.uuid4().hex
                now_iso = self._now()
                connection.execute(
                    "INSERT INTO rights_materials(material_id,site_id,work_id,title,source_type,source_description,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (material_id, site_id, work_id, title, source_type, source_description,
                     domain.MATERIAL_PENDING, actor_id, now_iso),
                )
                for holder in holders:
                    connection.execute(
                        "INSERT INTO rights_material_holders(material_id,holder_name,share_percent) VALUES(?,?,?)",
                        (material_id, holder["name"], holder["share_percent"]),
                    )
                license_id = self._insert_license(connection, material_id=material_id,
                                                  spec=license_spec, actor_id=actor_id, now_iso=now_iso)
                for spec in alternative_specs:
                    connection.execute(
                        "INSERT INTO rights_material_alternatives(material_id,alternative_material_id,note) VALUES(?,?,?)",
                        (material_id, spec["material_id"], spec["note"]),
                    )
                evidence_ids = []
                for spec in evidence_specs:
                    evidence_id, _, _ = self._insert_evidence(connection, material_id=material_id,
                                                              spec=spec, actor_id=actor_id, now_iso=now_iso)
                    evidence_ids.append(evidence_id)
                task_ids = []
                for task_type in (domain.TASK_AUTHENTICITY, domain.TASK_CONFLICT):
                    task_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO rights_verification_tasks(task_id,material_id,task_type,status,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (task_id, material_id, task_type, domain.TASK_PENDING, now_iso),
                    )
                    task_ids.append(task_id)
                append_event(connection, actor_id=actor_id, action="rights.material.registered",
                             resource_type="material", resource_id=material_id,
                             detail={"work_id": work_id, "title": title, "source_type": source_type,
                                     "license_id": license_id, "evidence_ids": evidence_ids,
                                     "verification_task_ids": task_ids},
                             occurred_at=now_iso)
                return {"resource_type": "material", "resource_id": material_id,
                        "material_id": material_id, "license_id": license_id,
                        "evidence_ids": evidence_ids, "verification_task_ids": task_ids}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.material.register", payload=payload, create=create)

    def upload_evidence(self, *, request_id: str, actor_id: str, material_id: str,
                        content: str, summary: str, sensitive: bool = False) -> dict[str, Any]:
        """上传证明文件；相同内容重复上传保持幂等，进入评审后补交会生成新结论。"""

        payload = {"actor_id": actor_id, "material_id": material_id, "content": content,
                   "summary": summary, "sensitive": bool(sensitive)}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, domain.REGISTER_ROLES)
            self._material_row(connection, material_id)
            spec = self._validate_evidence([{"content": content, "summary": summary,
                                             "sensitive": sensitive}])[0]

            def create() -> dict[str, Any]:
                now_iso = self._now()
                evidence_id, content_hash, duplicated = self._insert_evidence(
                    connection, material_id=material_id, spec=spec, actor_id=actor_id, now_iso=now_iso)
                refreshed = []
                if not duplicated:
                    append_event(connection, actor_id=actor_id, action="rights.evidence.uploaded",
                                 resource_type="evidence", resource_id=evidence_id,
                                 detail={"material_id": material_id, "content_hash": content_hash,
                                         "sensitive": spec["sensitive"]},
                                 occurred_at=now_iso)
                    refreshed = self._refresh_versions_for_materials(
                        connection, material_ids={material_id}, actor_id=actor_id,
                        reason="evidence_submitted")
                return {"resource_type": "evidence", "resource_id": evidence_id,
                        "evidence_id": evidence_id, "material_id": material_id,
                        "content_hash": content_hash, "deduplicated": duplicated,
                        "refreshed_versions": refreshed}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.evidence.upload", payload=payload, create=create)

    def submit_license(self, *, request_id: str, actor_id: str, material_id: str,
                       territory: str, usage_scope: str, valid_from: str,
                       valid_until: str | None = None,
                       required_attribution: str = "") -> dict[str, Any]:
        """补交授权：为素材追加一份许可；已进入评审的版本只生成新结论。"""

        payload = {"actor_id": actor_id, "material_id": material_id, "territory": territory,
                   "usage_scope": usage_scope, "valid_from": valid_from, "valid_until": valid_until,
                   "required_attribution": required_attribution}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, domain.REGISTER_ROLES | {domain.ROLE_LICENSING})
            self._material_row(connection, material_id)
            spec = self._validate_license({"territory": territory, "usage_scope": usage_scope,
                                           "valid_from": valid_from, "valid_until": valid_until,
                                           "required_attribution": required_attribution})

            def create() -> dict[str, Any]:
                now_iso = self._now()
                license_id = self._insert_license(connection, material_id=material_id,
                                                  spec=spec, actor_id=actor_id, now_iso=now_iso)
                append_event(connection, actor_id=actor_id, action="rights.license.submitted",
                             resource_type="license", resource_id=license_id,
                             detail={"material_id": material_id, "territory": spec["territory"],
                                     "usage_scope": spec["usage_scope"]},
                             occurred_at=now_iso)
                refreshed = self._refresh_versions_for_materials(
                    connection, material_ids={material_id}, actor_id=actor_id,
                    reason="license_submitted")
                return {"resource_type": "license", "resource_id": license_id,
                        "license_id": license_id, "material_id": material_id,
                        "refreshed_versions": refreshed}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.license.submit", payload=payload, create=create)

    # ---- 许可失效与影响分析 ----

    def _license_effective_status(self, license_row, now_iso: str) -> str:
        if license_row["status"] != domain.LICENSE_ACTIVE:
            return license_row["status"]
        valid_until = license_row["valid_until"]
        if valid_until and valid_until < now_iso:
            return domain.LICENSE_EXPIRED
        return domain.LICENSE_ACTIVE

    def _invalidate_licenses(self, connection, *, license_rows, status: str,
                             reason: str, actor_id: str, now_iso: str) -> set[str]:
        material_ids = set()
        for row in license_rows:
            connection.execute(
                "UPDATE rights_licenses SET status=?, invalidated_at=?, invalidate_reason=? WHERE license_id=?",
                (status, now_iso, reason, row["license_id"]),
            )
            append_event(connection, actor_id=actor_id, action=f"rights.license.{status}",
                         resource_type="license", resource_id=row["license_id"],
                         detail={"material_id": row["material_id"], "reason": reason},
                         occurred_at=now_iso)
            material_ids.add(row["material_id"])
        return material_ids

    def revoke_license(self, *, request_id: str, actor_id: str, license_id: str,
                       reason: str) -> dict[str, Any]:
        """撤回许可：生成新的权利结论，并报告受影响版本、评审阶段与未签署合作。"""

        payload = {"actor_id": actor_id, "license_id": license_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, domain.LICENSE_ROLES)
            license_row = self._license_row(connection, license_id)
            if license_row["status"] != domain.LICENSE_ACTIVE:
                raise ConflictError("许可已失效，不能重复撤回")
            reason = self._text(reason, "reason", 500)

            def create() -> dict[str, Any]:
                now_iso = self._now()
                material_ids = self._invalidate_licenses(
                    connection, license_rows=[license_row], status=domain.LICENSE_REVOKED,
                    reason=reason, actor_id=actor_id, now_iso=now_iso)
                refreshed = self._refresh_versions_for_materials(
                    connection, material_ids=material_ids, actor_id=actor_id,
                    reason="license_revoked")
                impact = self._impact_report(connection, material_ids=material_ids, refreshed=refreshed)
                return {"resource_type": "license", "resource_id": license_id,
                        "license_id": license_id, "status": domain.LICENSE_REVOKED,
                        "impact": impact}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.license.revoke", payload=payload, create=create)

    def expire_licenses(self, *, request_id: str, actor_id: str) -> dict[str, Any]:
        """把已经过期的许可标记为失效，并刷新受影响版本的结论。"""

        payload = {"actor_id": actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, domain.LICENSE_ROLES | {domain.ROLE_OPERATOR})

            def create() -> dict[str, Any]:
                now_iso = self._now()
                rows = connection.execute(
                    "SELECT * FROM rights_licenses WHERE status=? AND valid_until IS NOT NULL AND valid_until<?",
                    (domain.LICENSE_ACTIVE, now_iso),
                ).fetchall()
                material_ids = self._invalidate_licenses(
                    connection, license_rows=rows, status=domain.LICENSE_EXPIRED,
                    reason="许可期限届满", actor_id=actor_id, now_iso=now_iso)
                refreshed = self._refresh_versions_for_materials(
                    connection, material_ids=material_ids, actor_id=actor_id,
                    reason="license_expired")
                impact = self._impact_report(connection, material_ids=material_ids, refreshed=refreshed)
                return {"resource_type": "license_sweep", "resource_id": f"sweep-{request_id}",
                        "expired_license_ids": [row["license_id"] for row in rows],
                        "impact": impact}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.license.expire", payload=payload, create=create)

    def _impact_report(self, connection, *, material_ids: set[str],
                       refreshed: list[dict[str, Any]]) -> dict[str, Any]:
        """汇总许可失效影响：受影响作品版本、评审阶段、尚未签署的商业合作。"""

        refreshed_by_version = {item["version_id"]: item for item in refreshed}
        affected = []
        unsigned = []
        if material_ids:
            marks = ",".join("?" for _ in material_ids)
            versions = connection.execute(
                f"SELECT DISTINCT v.* FROM rights_work_versions v "
                f"JOIN rights_version_materials vm ON v.version_id=vm.version_id "
                f"WHERE vm.material_id IN ({marks}) ORDER BY v.work_id, v.version_no",
                tuple(material_ids),
            ).fetchall()
            for version in versions:
                entry = {"version_id": version["version_id"], "work_id": version["work_id"],
                         "version_no": version["version_no"], "stage": version["stage"],
                         "in_review": version["stage"] != domain.STAGE_DRAFT}
                refresh = refreshed_by_version.get(version["version_id"])
                if refresh:
                    entry["new_conclusion_id"] = refresh["conclusion_id"]
                    entry["usable"] = refresh["usable"]
                affected.append(entry)
            version_ids = [version["version_id"] for version in versions]
            if version_ids:
                version_marks = ",".join("?" for _ in version_ids)
                rows = connection.execute(
                    f"SELECT * FROM rights_collaborations WHERE version_id IN ({version_marks}) "
                    f"AND status=? ORDER BY created_at, collaboration_id",
                    (*version_ids, domain.COLLAB_NEGOTIATING),
                ).fetchall()
                unsigned = [{"collaboration_id": row["collaboration_id"],
                             "version_id": row["version_id"], "partner": row["partner"],
                             "channel": row["channel"], "status": row["status"]} for row in rows]
        return {"affected_versions": affected, "unsigned_collaborations": unsigned}

    def license_impact(self, actor_id: str, license_id: str) -> dict[str, Any]:
        """只读地预演某项许可失效后的影响范围。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, domain.IMPACT_ROLES)
        license_row = self._license_row(connection, license_id)
        impact = self._impact_report(connection, material_ids={license_row["material_id"]}, refreshed=[])
        return {"license_id": license_id, "material_id": license_row["material_id"],
                "license_status": license_row["status"], **impact}

    # ---- 核验 ----

    def decide_verification(self, *, request_id: str, actor_id: str, task_id: str,
                            decision: str, note: str = "") -> dict[str, Any]:
        """由对应职责人员完成真实性核验或冲突复核，两项职责必须分离。"""

        payload = {"actor_id": actor_id, "task_id": task_id, "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            task = self._task_row(connection, task_id)
            required_role = domain.TASK_ROLE[task["task_type"]]
            if actor.role != required_role:
                raise PermissionDenied("该核验职责不属于当前角色")
            material = self._material_row(connection, task["material_id"])
            if material["created_by"] == actor_id:
                raise PermissionDenied("不能核验自己登记的素材")
            if task["status"] != domain.TASK_PENDING:
                raise ConflictError("核验任务已有结论")
            if decision not in (domain.TASK_APPROVED, domain.TASK_REJECTED):
                raise ValidationError("decision 必须是 approved 或 rejected")
            sibling = connection.execute(
                "SELECT * FROM rights_verification_tasks WHERE material_id=? AND task_type!=?",
                (task["material_id"], task["task_type"]),
            ).fetchone()
            if sibling and sibling["status"] != domain.TASK_PENDING and sibling["decided_by"] == actor_id:
                raise PermissionDenied("真实性核验与冲突复核必须由不同人员完成")
            note = self._optional_text(note, "note")

            def create() -> dict[str, Any]:
                now_iso = self._now()
                connection.execute(
                    "UPDATE rights_verification_tasks SET status=?, decided_by=?, decided_at=?, decision_note=? "
                    "WHERE task_id=?",
                    (decision, actor_id, now_iso, note, task_id),
                )
                material_status = material["status"]
                sibling_now = connection.execute(
                    "SELECT * FROM rights_verification_tasks WHERE material_id=? AND task_type!=?",
                    (task["material_id"], task["task_type"]),
                ).fetchone()
                if decision == domain.TASK_REJECTED or sibling_now["status"] == domain.TASK_REJECTED:
                    material_status = domain.MATERIAL_REJECTED
                elif decision == domain.TASK_APPROVED and sibling_now["status"] == domain.TASK_APPROVED:
                    material_status = domain.MATERIAL_VERIFIED
                if material_status != material["status"]:
                    connection.execute("UPDATE rights_materials SET status=? WHERE material_id=?",
                                       (material_status, task["material_id"]))
                append_event(connection, actor_id=actor_id, action="rights.verification.decided",
                             resource_type="verification_task", resource_id=task_id,
                             detail={"material_id": task["material_id"], "task_type": task["task_type"],
                                     "decision": decision, "material_status": material_status},
                             occurred_at=now_iso)
                refreshed = []
                if material_status != material["status"]:
                    refreshed = self._refresh_versions_for_materials(
                        connection, material_ids={task["material_id"]}, actor_id=actor_id,
                        reason="verification_decided")
                return {"resource_type": "verification_task", "resource_id": task_id,
                        "task_id": task_id, "material_id": task["material_id"],
                        "decision": decision, "material_status": material_status,
                        "refreshed_versions": refreshed}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.verification.decide", payload=payload, create=create)

    def list_verification_tasks(self, actor_id: str, status: str | None = domain.TASK_PENDING,
                                task_type: str | None = None) -> list[dict[str, Any]]:
        """待核验队列；核验员与复核员只能看到各自职责的任务。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, domain.QUEUE_ROLES)
        if actor.role == domain.ROLE_VERIFIER:
            task_type = domain.TASK_AUTHENTICITY
        elif actor.role == domain.ROLE_REVIEWER:
            task_type = domain.TASK_CONFLICT
        clauses = ["1=1"]
        parameters: list[Any] = []
        if status:
            if status not in (domain.TASK_PENDING, domain.TASK_APPROVED, domain.TASK_REJECTED):
                raise ValidationError("status 无效")
            clauses.append("t.status=?")
            parameters.append(status)
        if task_type:
            if task_type not in domain.TASK_TYPES:
                raise ValidationError("task_type 无效")
            clauses.append("t.task_type=?")
            parameters.append(task_type)
        rows = connection.execute(
            f"SELECT t.*, m.title AS material_title, m.source_type AS source_type, m.work_id AS work_id "
            f"FROM rights_verification_tasks t JOIN rights_materials m ON t.material_id=m.material_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY t.created_at, t.task_id",
            parameters,
        ).fetchall()
        return [{"task_id": row["task_id"], "material_id": row["material_id"],
                 "material_title": row["material_title"], "source_type": row["source_type"],
                 "work_id": row["work_id"], "task_type": row["task_type"], "status": row["status"],
                 "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                 "created_at": row["created_at"]} for row in rows]

    # ---- 作品、团队与版本 ----

    def register_work(self, *, request_id: str, actor_id: str, site_id: str, work_id: str,
                      title: str, team: list[dict[str, Any]]) -> dict[str, Any]:
        """登记作品与联合创作团队（成员份额合计必须为 100）。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "work_id": work_id,
                   "title": title, "team": team}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, domain.REGISTER_ROLES)
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if actor.organization_id != site["organization_id"] and actor.role != domain.ROLE_ADMIN:
                raise PermissionDenied("不能写入其他组织的场所")
            work_id = self._identifier(work_id, "work_id")
            title = self._text(title, "title")
            members = self._validate_team(connection, team)

            def create() -> dict[str, Any]:
                now_iso = self._now()
                try:
                    connection.execute(
                        "INSERT INTO rights_works(work_id,site_id,title,representative_id,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (work_id, site_id, title, None, actor_id, now_iso),
                    )
                except Exception as exc:
                    raise ConflictError("作品编号已经存在") from exc
                for member in members:
                    connection.execute(
                        "INSERT INTO rights_work_members(work_id,member_id,share_percent) VALUES(?,?,?)",
                        (work_id, member["member_id"], member["share_percent"]),
                    )
                append_event(connection, actor_id=actor_id, action="rights.work.registered",
                             resource_type="work", resource_id=work_id,
                             detail={"title": title, "team": members}, occurred_at=now_iso)
                return {"resource_type": "work", "resource_id": work_id, "work_id": work_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.work.register", payload=payload, create=create)

    def _validate_team(self, connection, team: Any) -> list[dict[str, Any]]:
        if not isinstance(team, (list, tuple)) or not team:
            raise ValidationError("team 必须是非空列表")
        members = []
        total = 0
        seen = set()
        for item in team:
            if not isinstance(item, dict):
                raise ValidationError("团队成员条目必须是对象")
            member_id = self._identifier(item.get("member_id", ""), "team.member_id")
            if member_id in seen:
                raise ValidationError("团队成员重复")
            if connection.execute("SELECT 1 FROM actors WHERE actor_id=?", (member_id,)).fetchone() is None:
                raise NotFoundError(f"团队成员 {member_id} 不存在")
            share = item.get("share_percent")
            if not isinstance(share, int) or isinstance(share, bool) or share <= 0 or share > 100:
                raise ValidationError("share_percent 必须是 1 到 100 的整数")
            seen.add(member_id)
            total += share
            members.append({"member_id": member_id, "share_percent": share})
        if total != 100:
            raise ValidationError("团队份额合计必须等于 100")
        return members

    def set_team_representative(self, *, request_id: str, actor_id: str, work_id: str,
                                member_id: str) -> dict[str, Any]:
        """指定团队代表；只有团队代表才能代表团队签署后续渠道合作。"""

        payload = {"actor_id": actor_id, "work_id": work_id, "member_id": member_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, frozenset({domain.ROLE_ADMIN, domain.ROLE_OPERATOR}))
            self._work_row(connection, work_id)
            if self._active_freeze(connection, "work", work_id):
                raise ConflictError("作品处于冻结状态")
            member_id = self._identifier(member_id, "member_id")
            if connection.execute(
                "SELECT 1 FROM rights_work_members WHERE work_id=? AND member_id=?",
                (work_id, member_id),
            ).fetchone() is None:
                raise ValidationError("团队代表必须是团队成员")

            def create() -> dict[str, Any]:
                connection.execute("UPDATE rights_works SET representative_id=? WHERE work_id=?",
                                   (member_id, work_id))
                append_event(connection, actor_id=actor_id, action="rights.work.representative_set",
                             resource_type="work", resource_id=work_id,
                             detail={"member_id": member_id}, occurred_at=self._now())
                return {"resource_type": "work", "resource_id": work_id,
                        "work_id": work_id, "representative_id": member_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.work.set_representative", payload=payload, create=create)

    def create_version(self, *, request_id: str, actor_id: str, work_id: str,
                       version_no: int, material_ids: list[str]) -> dict[str, Any]:
        """登记作品版本及其使用的素材集合。"""

        payload = {"actor_id": actor_id, "work_id": work_id, "version_no": version_no,
                   "material_ids": list(material_ids or [])}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, domain.REGISTER_ROLES)
            self._work_row(connection, work_id)
            if self._active_freeze(connection, "work", work_id):
                raise ConflictError("作品处于冻结状态")
            if not isinstance(version_no, int) or isinstance(version_no, bool) or version_no < 1:
                raise ValidationError("version_no 必须是正整数")
            if not isinstance(material_ids, (list, tuple)) or not material_ids:
                raise ValidationError("material_ids 必须是非空列表")
            material_ids = [self._identifier(item, "material_id") for item in material_ids]
            if len(set(material_ids)) != len(material_ids):
                raise ValidationError("material_ids 存在重复")
            for material_id in material_ids:
                material = self._material_row(connection, material_id)
                if material["work_id"] != work_id:
                    raise ValidationError("素材不属于该作品")
                if self._active_freeze(connection, "material", material_id):
                    raise ConflictError(f"素材 {material_id} 处于冻结状态")

            def create() -> dict[str, Any]:
                version_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO rights_work_versions(version_id,work_id,version_no,stage,entered_review_at,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (version_id, work_id, version_no, domain.STAGE_DRAFT, None, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("作品版本号已经存在") from exc
                for material_id in material_ids:
                    connection.execute(
                        "INSERT INTO rights_version_materials(version_id,material_id) VALUES(?,?)",
                        (version_id, material_id),
                    )
                append_event(connection, actor_id=actor_id, action="rights.version.created",
                             resource_type="version", resource_id=version_id,
                             detail={"work_id": work_id, "version_no": version_no,
                                     "material_ids": list(material_ids)},
                             occurred_at=self._now())
                return {"resource_type": "version", "resource_id": version_id,
                        "version_id": version_id, "work_id": work_id, "version_no": version_no}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.version.create", payload=payload, create=create)

    def enter_review(self, *, request_id: str, actor_id: str, version_id: str,
                     stage: str) -> dict[str, Any]:
        """作品版本进入评审：冻结当时的权利依据并生成第一份权利结论。"""

        payload = {"actor_id": actor_id, "version_id": version_id, "stage": stage}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, frozenset({domain.ROLE_ADMIN, domain.ROLE_OPERATOR}))
            version = self._version_row(connection, version_id)
            if stage not in domain.REVIEW_STAGES:
                raise ValidationError("stage 必须是 preliminary、semi_final 或 final")
            if version["stage"] != domain.STAGE_DRAFT:
                raise ConflictError("作品版本已进入评审")
            if self._active_freeze(connection, "version", version_id):
                raise ConflictError("作品版本处于冻结状态")
            if self._active_freeze(connection, "work", version["work_id"]):
                raise ConflictError("作品处于冻结状态")
            materials = connection.execute(
                "SELECT m.* FROM rights_materials m JOIN rights_version_materials vm "
                "ON m.material_id=vm.material_id WHERE vm.version_id=? ORDER BY m.material_id",
                (version_id,),
            ).fetchall()
            unverified = [row["title"] for row in materials if row["status"] != domain.MATERIAL_VERIFIED]
            if unverified:
                raise ValidationError(f"存在未完成核验的素材：{'、'.join(unverified)}")
            for row in materials:
                if self._active_freeze(connection, "material", row["material_id"]):
                    raise ConflictError(f"素材 {row['title']} 处于冻结状态")

            def create() -> dict[str, Any]:
                now_iso = self._now()
                connection.execute(
                    "UPDATE rights_work_versions SET stage=?, entered_review_at=? WHERE version_id=?",
                    (stage, now_iso, version_id),
                )
                append_event(connection, actor_id=actor_id, action="rights.version.entered_review",
                             resource_type="version", resource_id=version_id,
                             detail={"stage": stage}, occurred_at=now_iso)
                conclusion = self._generate_conclusion(connection, version_id=version_id,
                                                       actor_id=actor_id, reason="enter_review")
                return {"resource_type": "version", "resource_id": version_id,
                        "version_id": version_id, "stage": stage, "conclusion": conclusion}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.version.enter_review", payload=payload, create=create)

    def advance_stage(self, *, request_id: str, actor_id: str, version_id: str,
                      stage: str) -> dict[str, Any]:
        """按 preliminary → semi_final → final 逐级推进评审阶段。"""

        payload = {"actor_id": actor_id, "version_id": version_id, "stage": stage}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, frozenset({domain.ROLE_ADMIN, domain.ROLE_OPERATOR}))
            version = self._version_row(connection, version_id)
            if stage not in domain.REVIEW_STAGES:
                raise ValidationError("stage 必须是 preliminary、semi_final 或 final")
            if version["stage"] == domain.STAGE_DRAFT:
                raise ConflictError("作品版本尚未进入评审")
            if domain.STAGE_ORDER[stage] != domain.STAGE_ORDER[version["stage"]] + 1:
                raise ConflictError("评审阶段必须逐级推进")
            if self._active_freeze(connection, "version", version_id):
                raise ConflictError("作品版本处于冻结状态")

            def create() -> dict[str, Any]:
                connection.execute("UPDATE rights_work_versions SET stage=? WHERE version_id=?",
                                   (stage, version_id))
                append_event(connection, actor_id=actor_id, action="rights.version.stage_advanced",
                             resource_type="version", resource_id=version_id,
                             detail={"stage": stage}, occurred_at=self._now())
                return {"resource_type": "version", "resource_id": version_id,
                        "version_id": version_id, "stage": stage}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.version.advance_stage", payload=payload, create=create)

    # ---- 权利结论 ----

    def _generate_conclusion(self, connection, *, version_id: str, actor_id: str,
                             reason: str) -> dict[str, Any]:
        """基于当前状态生成新的权利结论；历史结论及其依据快照不被改写。"""

        version = self._version_row(connection, version_id)
        now_iso = self._now()
        materials = connection.execute(
            "SELECT m.* FROM rights_materials m JOIN rights_version_materials vm "
            "ON m.material_id=vm.material_id WHERE vm.version_id=? ORDER BY m.material_id",
            (version_id,),
        ).fetchall()
        basis_materials = []
        problems = []
        attributions = set()
        for material in materials:
            holders = [{"name": row["holder_name"], "share_percent": row["share_percent"]}
                       for row in connection.execute(
                           "SELECT * FROM rights_material_holders WHERE material_id=? ORDER BY holder_name",
                           (material["material_id"],)).fetchall()]
            verifications = [{"task_id": row["task_id"], "task_type": row["task_type"],
                              "status": row["status"], "decided_by": row["decided_by"],
                              "decided_at": row["decided_at"], "decision_note": row["decision_note"]}
                             for row in connection.execute(
                                 "SELECT * FROM rights_verification_tasks WHERE material_id=? ORDER BY task_type",
                                 (material["material_id"],)).fetchall()]
            license_views = []
            active_found = False
            for row in connection.execute(
                    "SELECT * FROM rights_licenses WHERE material_id=? ORDER BY created_at, license_id",
                    (material["material_id"],)).fetchall():
                effective = self._license_effective_status(row, now_iso)
                if effective == domain.LICENSE_ACTIVE:
                    active_found = True
                    attributions.add(row["required_attribution"])
                license_views.append({"license_id": row["license_id"], "territory": row["territory"],
                                      "usage_scope": row["usage_scope"], "valid_from": row["valid_from"],
                                      "valid_until": row["valid_until"],
                                      "required_attribution": row["required_attribution"],
                                      "status": effective})
            evidence_views = [{"evidence_id": row["evidence_id"], "content_hash": row["content_hash"],
                               "sensitive": bool(row["sensitive"])}
                              for row in connection.execute(
                                  "SELECT evidence_id, content_hash, sensitive FROM rights_evidence "
                                  "WHERE material_id=? ORDER BY created_at, evidence_id",
                                  (material["material_id"],)).fetchall()]
            alternatives = [{"alternative_material_id": row["alternative_material_id"], "note": row["note"]}
                            for row in connection.execute(
                                "SELECT * FROM rights_material_alternatives WHERE material_id=? ORDER BY id",
                                (material["material_id"],)).fetchall()]
            frozen = self._active_freeze(connection, "material", material["material_id"]) is not None
            if material["status"] != domain.MATERIAL_VERIFIED:
                problems.append(f"素材《{material['title']}》未完成核验")
            if frozen:
                problems.append(f"素材《{material['title']}》处于冻结状态")
            if not active_found:
                problems.append(f"素材《{material['title']}》缺少有效许可")
            basis_materials.append({
                "material_id": material["material_id"], "title": material["title"],
                "source_type": material["source_type"], "status": material["status"],
                "frozen": frozen, "holders": holders, "verifications": verifications,
                "licenses": license_views, "evidence": evidence_views,
                "alternatives": alternatives})
        usable = not problems
        connection.execute(
            "UPDATE rights_conclusions SET standing=? WHERE version_id=? AND standing=?",
            (domain.STANDING_SUPERSEDED, version_id, domain.STANDING_CURRENT),
        )
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS next FROM rights_conclusions WHERE version_id=?",
            (version_id,),
        ).fetchone()["next"]
        basis = {"version_id": version_id, "work_id": version["work_id"], "stage": version["stage"],
                 "reason": reason, "generated_at": now_iso, "materials": basis_materials,
                 "problems": problems, "required_attributions": sorted(attributions)}
        basis_hash = digest(basis)
        conclusion_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO rights_conclusions(conclusion_id,version_id,sequence,standing,usable,reason,"
            "basis_json,basis_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (conclusion_id, version_id, sequence, domain.STANDING_CURRENT, 1 if usable else 0,
             reason, canonical_json(basis), basis_hash, actor_id, now_iso),
        )
        append_event(connection, actor_id=actor_id, action="rights.conclusion.generated",
                     resource_type="conclusion", resource_id=conclusion_id,
                     detail={"version_id": version_id, "sequence": sequence, "usable": usable,
                             "reason": reason, "basis_hash": basis_hash},
                     occurred_at=now_iso)
        return {"conclusion_id": conclusion_id, "version_id": version_id, "sequence": sequence,
                "usable": usable, "basis_hash": basis_hash, "problems": problems}

    def _refresh_versions_for_materials(self, connection, *, material_ids: Iterable[str],
                                        actor_id: str, reason: str) -> list[dict[str, Any]]:
        """为包含相关素材且已进入评审的版本生成新的权利结论。"""

        material_ids = set(material_ids)
        if not material_ids:
            return []
        marks = ",".join("?" for _ in material_ids)
        versions = connection.execute(
            f"SELECT DISTINCT v.* FROM rights_work_versions v "
            f"JOIN rights_version_materials vm ON v.version_id=vm.version_id "
            f"WHERE vm.material_id IN ({marks}) AND v.stage<>? ORDER BY v.version_id",
            (*material_ids, domain.STAGE_DRAFT),
        ).fetchall()
        refreshed = []
        for version in versions:
            conclusion = self._generate_conclusion(connection, version_id=version["version_id"],
                                                   actor_id=actor_id, reason=reason)
            refreshed.append({"version_id": version["version_id"], "work_id": version["work_id"],
                              "version_no": version["version_no"], "stage": version["stage"],
                              **conclusion})
        return refreshed

    def _conclusion_view(self, connection, row) -> dict[str, Any]:
        return {"conclusion_id": row["conclusion_id"], "version_id": row["version_id"],
                "sequence": row["sequence"], "standing": row["standing"],
                "usable": bool(row["usable"]), "reason": row["reason"],
                "basis_hash": row["basis_hash"], "created_by": row["created_by"],
                "created_at": row["created_at"],
                "frozen": self._active_freeze(connection, "conclusion", row["conclusion_id"]) is not None}

    def get_conclusion(self, actor_id: str, conclusion_id: str) -> dict[str, Any]:
        """读取结论及其依据快照；依据只含证据哈希，不含敏感摘要。"""

        connection = self.database.connection
        self._optional_actor(connection, actor_id)
        row = self._conclusion_row(connection, conclusion_id)
        view = self._conclusion_view(connection, row)
        view["basis"] = json.loads(row["basis_json"])
        return view

    def list_conclusions(self, actor_id: str, version_id: str) -> list[dict[str, Any]]:
        """列出一个版本的全部历史结论；进入评审前的依据链条完整可查。"""

        connection = self.database.connection
        self._optional_actor(connection, actor_id)
        self._version_row(connection, version_id)
        rows = connection.execute(
            "SELECT * FROM rights_conclusions WHERE version_id=? ORDER BY sequence", (version_id,)
        ).fetchall()
        return [self._conclusion_view(connection, row) for row in rows]

    def trace_conclusion_basis(self, actor_id: str, conclusion_id: str) -> dict[str, Any]:
        """授权专员追溯可用结论的完整依据：快照、证明、保全记录与审计链。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, domain.OFFICER_ROLES, "仅授权专员可以追溯结论的完整依据")
        row = self._conclusion_row(connection, conclusion_id)
        basis = json.loads(row["basis_json"])
        basis_hash_valid = digest(basis) == row["basis_hash"]
        evidence_ids = [item["evidence_id"] for material in basis["materials"]
                        for item in material["evidence"]]
        evidence = []
        preservations = []
        if evidence_ids:
            marks = ",".join("?" for _ in evidence_ids)
            evidence_rows = connection.execute(
                f"SELECT * FROM rights_evidence WHERE evidence_id IN ({marks}) "
                f"ORDER BY created_at, evidence_id",
                evidence_ids,
            ).fetchall()
            evidence = [self._evidence_view(actor, item) for item in evidence_rows]
            preservations = [
                {"objection_id": item["objection_id"], "evidence_id": item["evidence_id"],
                 "content_hash": item["content_hash"], "preserved_by": item["preserved_by"],
                 "preserved_at": item["preserved_at"]}
                for item in connection.execute(
                    f"SELECT * FROM rights_preservations WHERE evidence_id IN ({marks}) "
                    f"ORDER BY preserved_at, evidence_id",
                    evidence_ids,
                ).fetchall()]
        material_ids = [material["material_id"] for material in basis["materials"]]
        audit_ids = [conclusion_id, row["version_id"], *material_ids, *evidence_ids]
        marks = ",".join("?" for _ in audit_ids)
        audit_trail = [
            {"sequence": item["sequence"], "event_id": item["event_id"], "action": item["action"],
             "resource_type": item["resource_type"], "resource_id": item["resource_id"],
             "event_hash": item["event_hash"], "occurred_at": item["occurred_at"]}
            for item in connection.execute(
                f"SELECT * FROM audit_events WHERE resource_id IN ({marks}) ORDER BY sequence",
                audit_ids,
            ).fetchall()]
        return {"conclusion": self._conclusion_view(connection, row), "basis": basis,
                "basis_hash_valid": basis_hash_valid, "evidence": evidence,
                "preservations": preservations, "audit_trail": audit_trail}

    # ---- 异议 ----

    def file_objection(self, *, request_id: str, actor_id: str, target_type: str,
                       target_id: str, reason: str) -> dict[str, Any]:
        """异议立案：任何在岗人员都可以对素材、版本、结论、作品或合作提出异议。"""

        payload = {"actor_id": actor_id, "target_type": target_type,
                   "target_id": target_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id)
            if target_type not in domain.OBJECTION_TARGETS:
                raise ValidationError("target_type 无效")
            self._target_materials(connection, target_type, target_id)
            reason = self._text(reason, "reason", 500)

            def create() -> dict[str, Any]:
                objection_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO rights_objections(objection_id,target_type,target_id,reason,status,filed_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (objection_id, target_type, target_id, reason, domain.OBJECTION_FILED,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="rights.objection.filed",
                             resource_type="objection", resource_id=objection_id,
                             detail={"target_type": target_type, "target_id": target_id},
                             occurred_at=self._now())
                return {"resource_type": "objection", "resource_id": objection_id,
                        "objection_id": objection_id, "status": domain.OBJECTION_FILED}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.objection.file", payload=payload, create=create)

    def preserve_evidence(self, *, request_id: str, actor_id: str, objection_id: str) -> dict[str, Any]:
        """证据保全：把涉案素材的证明哈希快照固定下来，重复保全保持幂等。"""

        payload = {"actor_id": actor_id, "objection_id": objection_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, domain.CASE_ROLES)
            objection = self._objection_row(connection, objection_id)
            if objection["status"] not in domain.OBJECTION_OPEN:
                raise ConflictError("当前状态不能进行证据保全")
            material_ids = self._target_materials(connection, objection["target_type"],
                                                  objection["target_id"])

            def create() -> dict[str, Any]:
                now_iso = self._now()
                evidence_rows = []
                if material_ids:
                    marks = ",".join("?" for _ in material_ids)
                    evidence_rows = connection.execute(
                        f"SELECT * FROM rights_evidence WHERE material_id IN ({marks}) "
                        f"ORDER BY created_at, evidence_id",
                        tuple(material_ids),
                    ).fetchall()
                new_count = 0
                for row in evidence_rows:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO rights_preservations(objection_id,evidence_id,content_hash,"
                        "preserved_by,preserved_at) VALUES(?,?,?,?,?)",
                        (objection_id, row["evidence_id"], row["content_hash"], actor_id, now_iso),
                    )
                    new_count += cursor.rowcount
                new_status = (domain.OBJECTION_PRESERVED
                              if objection["status"] == domain.OBJECTION_FILED
                              else objection["status"])
                connection.execute("UPDATE rights_objections SET status=? WHERE objection_id=?",
                                   (new_status, objection_id))
                hashes = [row["content_hash"] for row in evidence_rows]
                append_event(connection, actor_id=actor_id, action="rights.objection.evidence_preserved",
                             resource_type="objection", resource_id=objection_id,
                             detail={"evidence_count": len(evidence_rows),
                                     "new_preservations": new_count, "content_hashes": hashes},
                             occurred_at=now_iso)
                return {"resource_type": "objection", "resource_id": objection_id,
                        "objection_id": objection_id, "status": new_status,
                        "evidence_count": len(evidence_rows), "new_preservations": new_count,
                        "content_hashes": hashes}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.objection.preserve", payload=payload, create=create)

    def freeze_targets(self, *, request_id: str, actor_id: str, objection_id: str,
                       targets: list[dict[str, Any]]) -> dict[str, Any]:
        """局部冻结：只冻结异议指定的素材、结论、版本或合作，其余部分照常运转。"""

        payload = {"actor_id": actor_id, "objection_id": objection_id, "targets": targets}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, domain.CASE_ROLES)
            objection = self._objection_row(connection, objection_id)
            if objection["status"] not in domain.OBJECTION_OPEN:
                raise ConflictError("当前状态不能进行冻结")
            if not isinstance(targets, (list, tuple)) or not targets:
                raise ValidationError("targets 必须是非空列表")
            specs = []
            for item in targets:
                if not isinstance(item, dict):
                    raise ValidationError("冻结对象条目必须是对象")
                target_type = item.get("target_type")
                if target_type not in domain.OBJECTION_TARGETS:
                    raise ValidationError("target_type 无效")
                target_id = self._identifier(item.get("target_id", ""), "target_id")
                scope_note = self._text(item.get("scope_note", ""), "scope_note", 200)
                self._target_materials(connection, target_type, target_id)
                specs.append({"target_type": target_type, "target_id": target_id,
                              "scope_note": scope_note})

            def create() -> dict[str, Any]:
                now_iso = self._now()
                freeze_ids = []
                affected: set[str] = set()
                for spec in specs:
                    if self._active_freeze(connection, spec["target_type"], spec["target_id"]):
                        raise ConflictError(f"对象 {spec['target_id']} 已处于冻结状态")
                    freeze_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO rights_freezes(freeze_id,objection_id,target_type,target_id,scope_note,"
                        "status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (freeze_id, objection_id, spec["target_type"], spec["target_id"],
                         spec["scope_note"], domain.FREEZE_ACTIVE, actor_id, now_iso),
                    )
                    freeze_ids.append(freeze_id)
                    affected |= self._target_materials(connection, spec["target_type"], spec["target_id"])
                connection.execute("UPDATE rights_objections SET status=? WHERE objection_id=?",
                                   (domain.OBJECTION_FROZEN, objection_id))
                append_event(connection, actor_id=actor_id, action="rights.objection.frozen",
                             resource_type="objection", resource_id=objection_id,
                             detail={"freeze_ids": freeze_ids, "targets": specs},
                             occurred_at=now_iso)
                refreshed = self._refresh_versions_for_materials(
                    connection, material_ids=affected, actor_id=actor_id, reason="freeze_applied")
                return {"resource_type": "objection", "resource_id": objection_id,
                        "objection_id": objection_id, "status": domain.OBJECTION_FROZEN,
                        "freeze_ids": freeze_ids, "refreshed_versions": refreshed}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.objection.freeze", payload=payload, create=create)

    def _lift_objection_freezes(self, connection, objection_id: str, now_iso: str) -> set[str]:
        affected: set[str] = set()
        rows = connection.execute(
            "SELECT * FROM rights_freezes WHERE objection_id=? AND status=?",
            (objection_id, domain.FREEZE_ACTIVE),
        ).fetchall()
        for row in rows:
            affected |= self._target_materials(connection, row["target_type"], row["target_id"])
            connection.execute(
                "UPDATE rights_freezes SET status=?, lifted_at=? WHERE freeze_id=?",
                (domain.FREEZE_LIFTED, now_iso, row["freeze_id"]),
            )
        return affected

    def settle_objection(self, *, request_id: str, actor_id: str, objection_id: str,
                         resolution: str) -> dict[str, Any]:
        """和解结案：记录和解结果并解除本案的冻结。"""

        payload = {"actor_id": actor_id, "objection_id": objection_id, "resolution": resolution}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, domain.CASE_ROLES)
            objection = self._objection_row(connection, objection_id)
            if objection["status"] not in domain.OBJECTION_OPEN:
                raise ConflictError("异议已经结案")
            resolution = self._text(resolution, "resolution", 500)

            def create() -> dict[str, Any]:
                now_iso = self._now()
                affected = self._lift_objection_freezes(connection, objection_id, now_iso)
                connection.execute(
                    "UPDATE rights_objections SET status=?, resolution=?, resolved_at=? WHERE objection_id=?",
                    (domain.OBJECTION_SETTLED, resolution, now_iso, objection_id),
                )
                append_event(connection, actor_id=actor_id, action="rights.objection.settled",
                             resource_type="objection", resource_id=objection_id,
                             detail={"resolution": resolution}, occurred_at=now_iso)
                refreshed = self._refresh_versions_for_materials(
                    connection, material_ids=affected, actor_id=actor_id, reason="settlement")
                return {"resource_type": "objection", "resource_id": objection_id,
                        "objection_id": objection_id, "status": domain.OBJECTION_SETTLED,
                        "refreshed_versions": refreshed}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.objection.settle", payload=payload, create=create)

    def adjudicate_objection(self, *, request_id: str, actor_id: str, objection_id: str,
                             outcome: str, resolution: str) -> dict[str, Any]:
        """裁定结案：驳回异议，或裁定成立并使相关素材/结论失效。"""

        payload = {"actor_id": actor_id, "objection_id": objection_id,
                   "outcome": outcome, "resolution": resolution}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, domain.CASE_ROLES)
            objection = self._objection_row(connection, objection_id)
            if objection["status"] not in domain.OBJECTION_OPEN:
                raise ConflictError("异议已经结案")
            if outcome not in domain.ADJUDICATION_OUTCOMES:
                raise ValidationError("outcome 必须是 dismissed 或 sustained")
            resolution = self._text(resolution, "resolution", 500)

            def create() -> dict[str, Any]:
                now_iso = self._now()
                affected = self._target_materials(connection, objection["target_type"],
                                                  objection["target_id"])
                invalidated_conclusion_id = None
                if outcome == "sustained":
                    if objection["target_type"] == "conclusion":
                        conclusion = self._conclusion_row(connection, objection["target_id"])
                        if conclusion["standing"] != domain.STANDING_CURRENT:
                            raise ConflictError("该结论已非当前结论")
                        connection.execute(
                            "UPDATE rights_conclusions SET standing=? WHERE conclusion_id=?",
                            (domain.STANDING_INVALIDATED, conclusion["conclusion_id"]),
                        )
                        invalidated_conclusion_id = conclusion["conclusion_id"]
                    elif objection["target_type"] == "material":
                        connection.execute("UPDATE rights_materials SET status=? WHERE material_id=?",
                                           (domain.MATERIAL_REJECTED, objection["target_id"]))
                affected |= self._lift_objection_freezes(connection, objection_id, now_iso)
                connection.execute(
                    "UPDATE rights_objections SET status=?, resolution=?, resolved_at=? WHERE objection_id=?",
                    (domain.OBJECTION_ADJUDICATED, resolution, now_iso, objection_id),
                )
                append_event(connection, actor_id=actor_id, action="rights.objection.adjudicated",
                             resource_type="objection", resource_id=objection_id,
                             detail={"outcome": outcome, "resolution": resolution,
                                     "invalidated_conclusion_id": invalidated_conclusion_id},
                             occurred_at=now_iso)
                refreshed = self._refresh_versions_for_materials(
                    connection, material_ids=affected, actor_id=actor_id, reason="adjudication")
                return {"resource_type": "objection", "resource_id": objection_id,
                        "objection_id": objection_id, "status": domain.OBJECTION_ADJUDICATED,
                        "outcome": outcome, "invalidated_conclusion_id": invalidated_conclusion_id,
                        "refreshed_versions": refreshed}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.objection.adjudicate", payload=payload, create=create)

    # ---- 商业合作 ----

    def create_collaboration(self, *, request_id: str, actor_id: str, version_id: str,
                             partner: str, channel: str) -> dict[str, Any]:
        """为进入评审的作品版本登记一项渠道合作（尚未签署）。"""

        payload = {"actor_id": actor_id, "version_id": version_id,
                   "partner": partner, "channel": channel}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, domain.COLLAB_CREATE_ROLES)
            version = self._version_row(connection, version_id)
            if version["stage"] == domain.STAGE_DRAFT:
                raise ValidationError("作品版本尚未进入评审")
            if self._active_freeze(connection, "version", version_id):
                raise ConflictError("作品版本处于冻结状态")
            if self._active_freeze(connection, "work", version["work_id"]):
                raise ConflictError("作品处于冻结状态")
            partner = self._text(partner, "partner", 120)
            channel = self._text(channel, "channel", 120)

            def create() -> dict[str, Any]:
                collaboration_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO rights_collaborations(collaboration_id,version_id,partner,channel,status,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (collaboration_id, version_id, partner, channel,
                     domain.COLLAB_NEGOTIATING, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="rights.collaboration.created",
                             resource_type="collaboration", resource_id=collaboration_id,
                             detail={"version_id": version_id, "partner": partner, "channel": channel},
                             occurred_at=self._now())
                return {"resource_type": "collaboration", "resource_id": collaboration_id,
                        "collaboration_id": collaboration_id, "version_id": version_id,
                        "status": domain.COLLAB_NEGOTIATING}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.collaboration.create", payload=payload, create=create)

    def sign_collaboration(self, *, request_id: str, actor_id: str,
                           collaboration_id: str) -> dict[str, Any]:
        """团队代表签署渠道合作；权利结论不可用或被冻结时不能签署。"""

        payload = {"actor_id": actor_id, "collaboration_id": collaboration_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            collaboration = self._collaboration_row(connection, collaboration_id)
            if collaboration["status"] != domain.COLLAB_NEGOTIATING:
                raise ConflictError("合作已签署")
            version = self._version_row(connection, collaboration["version_id"])
            work = self._work_row(connection, version["work_id"])
            if not work["representative_id"]:
                raise ConflictError("作品尚未指定团队代表")
            if work["representative_id"] != actor_id:
                raise PermissionDenied("仅团队代表可以代表团队签署渠道合作")
            if self._active_freeze(connection, "collaboration", collaboration_id):
                raise ConflictError("合作处于冻结状态")
            if self._active_freeze(connection, "version", version["version_id"]):
                raise ConflictError("作品版本处于冻结状态")
            conclusion = connection.execute(
                "SELECT * FROM rights_conclusions WHERE version_id=? AND standing=?",
                (version["version_id"], domain.STANDING_CURRENT),
            ).fetchone()
            if conclusion is None or not conclusion["usable"]:
                raise ConflictError("当前权利结论不可用，不能签署合作")
            if self._active_freeze(connection, "conclusion", conclusion["conclusion_id"]):
                raise ConflictError("当前权利结论处于冻结状态")

            def create() -> dict[str, Any]:
                now_iso = self._now()
                connection.execute(
                    "UPDATE rights_collaborations SET status=?, signed_by=?, signed_at=? "
                    "WHERE collaboration_id=?",
                    (domain.COLLAB_SIGNED, actor_id, now_iso, collaboration_id),
                )
                append_event(connection, actor_id=actor_id, action="rights.collaboration.signed",
                             resource_type="collaboration", resource_id=collaboration_id,
                             detail={"version_id": version["version_id"]}, occurred_at=now_iso)
                return {"resource_type": "collaboration", "resource_id": collaboration_id,
                        "collaboration_id": collaboration_id, "status": domain.COLLAB_SIGNED}

            return self._idempotent(connection, request_id=request_id,
                                    action="rights.collaboration.sign", payload=payload, create=create)

    # ---- 查询 ----

    def _evidence_view(self, actor: Actor | None, row) -> dict[str, Any]:
        view = {"evidence_id": row["evidence_id"], "material_id": row["material_id"],
                "content_hash": row["content_hash"], "sensitive": bool(row["sensitive"]),
                "uploaded_by": row["uploaded_by"], "created_at": row["created_at"]}
        privileged = actor is not None and (
            actor.role in domain.SENSITIVE_READ_ROLES or actor.actor_id == row["uploaded_by"])
        if not row["sensitive"] or privileged:
            view["summary"] = row["summary"]
            view["content"] = row["content"]
        else:
            view["summary"] = "[受限]"
            view["content"] = None
        return view

    def get_material(self, actor_id: str, material_id: str) -> dict[str, Any]:
        """素材详情；敏感证明对评委等无关人员只显示哈希。"""

        connection = self.database.connection
        actor = self._optional_actor(connection, actor_id)
        material = self._material_row(connection, material_id)
        holders = [{"name": row["holder_name"], "share_percent": row["share_percent"]}
                   for row in connection.execute(
                       "SELECT * FROM rights_material_holders WHERE material_id=? ORDER BY holder_name",
                       (material_id,)).fetchall()]
        now_iso = self._now()
        licenses = [{"license_id": row["license_id"], "territory": row["territory"],
                     "usage_scope": row["usage_scope"], "valid_from": row["valid_from"],
                     "valid_until": row["valid_until"],
                     "required_attribution": row["required_attribution"],
                     "status": self._license_effective_status(row, now_iso),
                     "invalidated_at": row["invalidated_at"],
                     "invalidate_reason": row["invalidate_reason"]}
                    for row in connection.execute(
                        "SELECT * FROM rights_licenses WHERE material_id=? ORDER BY created_at, license_id",
                        (material_id,)).fetchall()]
        alternatives = [{"alternative_material_id": row["alternative_material_id"], "note": row["note"]}
                        for row in connection.execute(
                            "SELECT * FROM rights_material_alternatives WHERE material_id=? ORDER BY id",
                            (material_id,)).fetchall()]
        evidence = [self._evidence_view(actor, row)
                    for row in connection.execute(
                        "SELECT * FROM rights_evidence WHERE material_id=? ORDER BY created_at, evidence_id",
                        (material_id,)).fetchall()]
        verifications = [{"task_id": row["task_id"], "task_type": row["task_type"],
                          "status": row["status"], "decided_by": row["decided_by"],
                          "decided_at": row["decided_at"], "decision_note": row["decision_note"]}
                         for row in connection.execute(
                             "SELECT * FROM rights_verification_tasks WHERE material_id=? ORDER BY task_type",
                             (material_id,)).fetchall()]
        return {"material_id": material_id, "site_id": material["site_id"],
                "work_id": material["work_id"], "title": material["title"],
                "source_type": material["source_type"],
                "source_description": material["source_description"],
                "status": material["status"], "created_by": material["created_by"],
                "created_at": material["created_at"],
                "frozen": self._active_freeze(connection, "material", material_id) is not None,
                "holders": holders, "licenses": licenses, "alternatives": alternatives,
                "evidence": evidence, "verifications": verifications}

    def get_work(self, actor_id: str, work_id: str) -> dict[str, Any]:
        connection = self.database.connection
        self._optional_actor(connection, actor_id)
        work = self._work_row(connection, work_id)
        team = [{"member_id": row["member_id"], "share_percent": row["share_percent"]}
                for row in connection.execute(
                    "SELECT * FROM rights_work_members WHERE work_id=? ORDER BY member_id",
                    (work_id,)).fetchall()]
        versions = [{"version_id": row["version_id"], "version_no": row["version_no"],
                     "stage": row["stage"]}
                    for row in connection.execute(
                        "SELECT * FROM rights_work_versions WHERE work_id=? ORDER BY version_no",
                        (work_id,)).fetchall()]
        return {"work_id": work_id, "site_id": work["site_id"], "title": work["title"],
                "representative_id": work["representative_id"],
                "frozen": self._active_freeze(connection, "work", work_id) is not None,
                "team": team, "versions": versions, "created_at": work["created_at"]}

    def get_version(self, actor_id: str, version_id: str) -> dict[str, Any]:
        connection = self.database.connection
        self._optional_actor(connection, actor_id)
        version = self._version_row(connection, version_id)
        work = self._work_row(connection, version["work_id"])
        materials = [{"material_id": row["material_id"], "title": row["title"],
                      "status": row["status"],
                      "frozen": self._active_freeze(connection, "material", row["material_id"]) is not None}
                     for row in connection.execute(
                         "SELECT m.* FROM rights_materials m JOIN rights_version_materials vm "
                         "ON m.material_id=vm.material_id WHERE vm.version_id=? ORDER BY m.material_id",
                         (version_id,)).fetchall()]
        conclusion = connection.execute(
            "SELECT * FROM rights_conclusions WHERE version_id=? AND standing=?",
            (version_id, domain.STANDING_CURRENT),
        ).fetchone()
        conclusion_count = connection.execute(
            "SELECT COUNT(*) AS count FROM rights_conclusions WHERE version_id=?", (version_id,)
        ).fetchone()["count"]
        collaborations = [{"collaboration_id": row["collaboration_id"], "partner": row["partner"],
                           "channel": row["channel"], "status": row["status"]}
                          for row in connection.execute(
                              "SELECT * FROM rights_collaborations WHERE version_id=? ORDER BY created_at",
                              (version_id,)).fetchall()]
        return {"version_id": version_id, "work_id": version["work_id"],
                "work_title": work["title"], "version_no": version["version_no"],
                "stage": version["stage"], "entered_review_at": version["entered_review_at"],
                "frozen": self._active_freeze(connection, "version", version_id) is not None,
                "materials": materials,
                "current_conclusion": self._conclusion_view(connection, conclusion) if conclusion else None,
                "conclusion_count": conclusion_count, "collaborations": collaborations}

    def list_objections(self, actor_id: str, status: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        clauses = ["1=1"]
        parameters: list[Any] = []
        if status:
            clauses.append("status=?")
            parameters.append(status)
        rows = connection.execute(
            f"SELECT * FROM rights_objections WHERE {' AND '.join(clauses)} ORDER BY created_at, objection_id",
            parameters,
        ).fetchall()
        return [{"objection_id": row["objection_id"], "target_type": row["target_type"],
                 "target_id": row["target_id"], "reason": row["reason"], "status": row["status"],
                 "filed_by": row["filed_by"], "created_at": row["created_at"],
                 "resolved_at": row["resolved_at"]} for row in rows]

    def get_objection(self, actor_id: str, objection_id: str) -> dict[str, Any]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        row = self._objection_row(connection, objection_id)
        preservations = [{"evidence_id": item["evidence_id"], "content_hash": item["content_hash"],
                          "preserved_by": item["preserved_by"], "preserved_at": item["preserved_at"]}
                         for item in connection.execute(
                             "SELECT * FROM rights_preservations WHERE objection_id=? "
                             "ORDER BY preserved_at, evidence_id",
                             (objection_id,)).fetchall()]
        freezes = [{"freeze_id": item["freeze_id"], "target_type": item["target_type"],
                    "target_id": item["target_id"], "scope_note": item["scope_note"],
                    "status": item["status"], "created_at": item["created_at"],
                    "lifted_at": item["lifted_at"]}
                   for item in connection.execute(
                       "SELECT * FROM rights_freezes WHERE objection_id=? ORDER BY created_at, freeze_id",
                       (objection_id,)).fetchall()]
        return {"objection_id": objection_id, "target_type": row["target_type"],
                "target_id": row["target_id"], "reason": row["reason"], "status": row["status"],
                "filed_by": row["filed_by"], "created_at": row["created_at"],
                "resolution": row["resolution"], "resolved_at": row["resolved_at"],
                "preservations": preservations, "freezes": freezes}

    def list_collaborations(self, actor_id: str, version_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._optional_actor(connection, actor_id)
        clauses = ["1=1"]
        parameters: list[Any] = []
        if version_id:
            clauses.append("version_id=?")
            parameters.append(version_id)
        rows = connection.execute(
            f"SELECT * FROM rights_collaborations WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at, collaboration_id",
            parameters,
        ).fetchall()
        return [{"collaboration_id": row["collaboration_id"], "version_id": row["version_id"],
                 "partner": row["partner"], "channel": row["channel"], "status": row["status"],
                 "created_by": row["created_by"], "created_at": row["created_at"],
                 "signed_by": row["signed_by"], "signed_at": row["signed_at"]} for row in rows]

    # ---- 中断恢复一致性 ----

    def verify_recovery(self, actor_id: str) -> dict[str, Any]:
        """服务恢复后校验：待核验队列、证据哈希、保全哈希、结论依据与审计链。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, domain.RECOVERY_ROLES)
        pending = connection.execute(
            "SELECT task_id FROM rights_verification_tasks WHERE status=? ORDER BY created_at, task_id",
            (domain.TASK_PENDING,),
        ).fetchall()
        evidence_mismatches = []
        evidence_total = 0
        for row in connection.execute("SELECT evidence_id, content, content_hash FROM rights_evidence"):
            evidence_total += 1
            if self._evidence_hash(row["content"]) != row["content_hash"]:
                evidence_mismatches.append(row["evidence_id"])
        preservation_mismatches = []
        for row in connection.execute(
                "SELECT p.objection_id, p.evidence_id, p.content_hash, e.content_hash AS current_hash "
                "FROM rights_preservations p JOIN rights_evidence e ON p.evidence_id=e.evidence_id"):
            if row["content_hash"] != row["current_hash"]:
                preservation_mismatches.append(
                    {"objection_id": row["objection_id"], "evidence_id": row["evidence_id"]})
        basis_mismatches = []
        for row in connection.execute("SELECT conclusion_id, basis_json, basis_hash FROM rights_conclusions"):
            if digest(json.loads(row["basis_json"])) != row["basis_hash"]:
                basis_mismatches.append(row["conclusion_id"])
        audit_valid, audit_events = verify_chain(connection)
        consistent = (not evidence_mismatches and not preservation_mismatches
                      and not basis_mismatches and audit_valid)
        return {"pending_tasks": len(pending),
                "pending_task_ids": [row["task_id"] for row in pending],
                "evidence_total": evidence_total,
                "evidence_hash_mismatches": evidence_mismatches,
                "preservation_mismatches": preservation_mismatches,
                "conclusion_basis_mismatches": basis_mismatches,
                "audit_valid": audit_valid, "audit_events": audit_events,
                "consistent": consistent}
