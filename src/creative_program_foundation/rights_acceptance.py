"""文化素材权利链核验域的离线端到端验收。

以“茶礼作品同时引用老照片、书法拓片、社区口述故事”为场景，覆盖：

素材/权利人/份额/许可要素登记 → 证据上传与哈希幂等 → 真实性核验与冲突复核
→ 版本进入评审并冻结依据 → 复赛后补交授权只追加新结论 → 许可到期影响面
（受影响版本、评审阶段、尚未签署的商业合作）→ 异议立案、证据保全、
局部冻结、和解 → 评委脱敏、授权专员追溯 → 重启后队列与哈希一致 →
审计哈希链校验。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .rights_schema import RIGHTS_SCHEMA
from .rights_service import RightsService
from .storage import Database

CLOCK = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))


def _build(service: RightsService) -> dict[str, object]:
    # 组织与四类职责人员
    service.register_organization(request_id="acc-org", actor_id="bootstrap",
                                  organization_id="org-tea", name="文创大赛组委会")
    service.register_actor(request_id="acc-admin", actor_id="bootstrap",
                           new_actor_id="admin", display_name="管理员", role="admin",
                           organization_id="org-tea")
    for actor_id, name, role in [
        ("lic", "授权专员", "licensing_officer"),
        ("rev", "冲突复核员", "conflict_reviewer"),
        ("judge", "复赛评委", "judge"),
        ("creator", "茶礼主创", "creator"),
        ("ops", "渠道运营", "operator"),
    ]:
        service.register_actor(request_id=f"acc-actor-{actor_id}", actor_id="admin",
                               new_actor_id=actor_id, display_name=name, role=role,
                               organization_id="org-tea")
    service.register_site(request_id="acc-site", actor_id="admin", site_id="site-tea",
                          organization_id="org-tea", name="茶礼赛道",
                          timezone_name="Asia/Shanghai")

    # 作品与权利人（联合作者张三、书法世家声称可代表团队接受渠道合作）
    service.register_work(request_id="acc-work", actor_id="creator", site_id="site-tea",
                          work_id="tea-gift", title="岁时茶礼",
                          primary_creator_id="creator")
    holders = [
        ("h-archive", "市档案馆", "holder", "授权窗口", False),
        ("h-li", "书法世家李氏", "both", "拓片保管人", True),
        ("h-wang", "社区长者王婆", "holder", "口述项目组转交", False),
        ("h-zhang", "联合作者张三", "both", "团队成员", True),
    ]
    for holder_id, name, kind, contact, can_deal in holders:
        service.register_rights_holder(
            request_id=f"acc-holder-{holder_id}", actor_id="creator", holder_id=holder_id,
            display_name=name, kind=kind, contact_summary=contact,
            can_accept_channel_deals=can_deal)
    materials = [
        ("m-photo", "photo", "老照片：茶号旧影", "市档案馆藏民国茶号照片",
         ["h-archive"], {"h-archive": 100}, [], "*", ["exhibition", "merchandise"],
         "2026-01-01T00:00:00+08:00", "2027-12-31T23:59:59+08:00", "图源：市档案馆", None),
        ("m-rubbing", "rubbing", "书法拓片：茶帖", "李氏家藏碑帖拓本",
         ["h-li", "h-zhang"], {"h-li": 70, "h-zhang": 30}, ["h-li", "h-zhang"], "CN",
         ["exhibition", "publication"], "2026-01-01T00:00:00+08:00",
         "2026-10-01T23:59:59+08:00", "拓片：李氏家藏", None),
        ("m-story", "oral_story", "口述故事：制茶节令", "王婆口述录音整理",
         ["h-wang", "h-zhang"], {"h-wang": 60, "h-zhang": 40}, ["h-zhang"], "*",
         ["exhibition", "broadcast"], "2026-01-01T00:00:00+08:00", None,
         "口述：王婆（社区口述史项目）", None),
    ]
    for (mid, kind, title, source, hids, shares, coids, territory, purposes,
         vf, vu, attr, alt) in materials:
        service.register_material(
            request_id=f"acc-mat-{mid}", actor_id="creator", work_id="tea-gift",
            material_id=mid, kind=kind, title=title, source_description=source,
            rights_holder_ids=hids, holder_shares=shares, coauthor_ids=coids,
            territory=territory, purposes=purposes, valid_from=vf, valid_until=vu,
            required_attribution=attr, alternative_material_id=alt)

    # 证据：敏感授权书 + 普通来源说明；同内容重复上传必须幂等
    first = service.upload_evidence(
        request_id="acc-ev-photo", actor_id="creator", material_id="m-photo",
        evidence_id="ev-photo", filename="archive-license.pdf",
        media_type="application/pdf", summary="档案馆授权书（含联系人与底价，敏感）",
        classification="sensitive", content_base64="cG90b2xpY2Vuc2U=")
    replay = service.upload_evidence(
        request_id="acc-ev-photo-replay", actor_id="creator", material_id="m-photo",
        evidence_id="ev-photo-copy", filename="copy.pdf", media_type="application/pdf",
        summary="重复上传同一份", content_base64="cG90b2xpY2Vuc2U=")
    service.upload_evidence(
        request_id="acc-ev-rubbing", actor_id="creator", material_id="m-rubbing",
        evidence_id="ev-rubbing", filename="li-deed.pdf", media_type="application/pdf",
        summary="李氏拓片授权书", content_base64="cnViaW5nZGVlZA==")
    service.upload_evidence(
        request_id="acc-ev-story", actor_id="creator", material_id="m-story",
        evidence_id="ev-story", filename="wang-release.pdf", media_type="application/pdf",
        summary="王婆口述故事同意书", content_base64="d2FuZ3JlbGVhc2U=")

    # 真实性核验：老照片/拓片/口述均通过，各形成 cleared 结论
    for mid in ["m-photo", "m-rubbing", "m-story"]:
        tid = f"task-{mid}"
        service.create_verification_task(request_id=f"acc-{tid}", actor_id="lic",
                                         material_id=mid, task_id=tid)
        service.submit_verification(request_id=f"acc-{tid}-submit", actor_id="lic",
                                    task_id=tid, decision="authentic",
                                    notes="授权书与来源链条一致")
    # 再留一个待核验任务，用于演示服务重启后待核验队列不丢
    service.create_verification_task(request_id="acc-task-pending", actor_id="lic",
                                     material_id="m-story", task_id="task-pending-restart")

    # 版本 v1 进入初赛：固化当时三份素材结论的依据
    service.create_work_version(request_id="acc-v1", actor_id="creator",
                                work_id="tea-gift", version_id="v1",
                                material_ids=["m-photo", "m-rubbing", "m-story"])
    service.enter_review(request_id="acc-enter-prelim", actor_id="lic",
                         work_id="tea-gift", stage="preliminary", version_id="v1")
    pinned_before = service.get_version("v1").snapshot["m-photo"]

    # 复赛后补交老照片展期授权：只追加新结论（restricted），不抹掉初赛依据
    service.upload_evidence(
        request_id="acc-ev-photo-2027", actor_id="creator", material_id="m-photo",
        evidence_id="ev-photo-2027", filename="extension.pdf",
        media_type="application/pdf", summary="补交的展期授权",
        content_base64="ZXh0ZW5zaW9u")
    service.supplement_authorization(
        request_id="acc-supplement", actor_id="lic", material_id="m-photo",
        decision="restricted", note="仅展览用途展期")

    # 商业合作：出版合作待签（拓片到期后将受影响）；广播合作在异议冻结期间被阻断
    service.register_deal(request_id="acc-deal-book", actor_id="ops", deal_id="deal-book",
                          work_id="tea-gift", partner="国风出版社",
                          requires_purposes=["publication"], territory="CN")
    service.register_deal(request_id="acc-deal-radio", actor_id="ops", deal_id="deal-radio",
                          work_id="tea-gift", partner="地方电台",
                          requires_purposes=["broadcast"], territory="*")

    # 拓片商业化期限 2026-10-01 到期：扫描形成 expired 结论并给出影响面
    sweep = service.sweep_expired_authorizations(
        actor_id="lic", at="2026-10-02T00:00:00+08:00")

    # 口述故事异议：立案 → 保全王婆同意书哈希 → 对 v1 的口述素材局部冻结
    service.file_dispute(request_id="acc-dispute", actor_id="rev", dispute_id="dip-story",
                         work_id="tea-gift", material_id="m-story",
                         reason="王婆家属主张口述故事继承权益")
    service.preserve_evidence(request_id="acc-preserve", actor_id="lic",
                              dispute_id="dip-story", evidence_ids=["ev-story"])
    service.partial_freeze(request_id="acc-freeze", actor_id="rev",
                           dispute_id="dip-story", version_id="v1", material_id="m-story")
    # 冻结期间广播合作不能签署
    sign_blocked = False
    try:
        service.sign_deal(request_id="acc-sign-radio", actor_id="lic", deal_id="deal-radio")
    except Exception:
        sign_blocked = True
    # 和解：追加家属署名与收益分成，素材降级为 restricted，冻结解除
    service.resolve_dispute(
        request_id="acc-resolve", actor_id="rev", dispute_id="dip-story",
        outcome="settled", terms="追加家属署名，商业化收益分成 10%",
        new_decision="restricted")
    signed = service.sign_deal(request_id="acc-sign-radio-2", actor_id="lic",
                               deal_id="deal-radio")

    # 评委只能看到脱敏证明；授权专员可追溯每个结论的完整依据
    judge_pack = service.review_pack(actor_id="judge", work_id="tea-gift")
    judge_redacted = all(
        "已隐藏" in e["summary"]
        for m in judge_pack["materials"] for e in m["evidence"]
        if e["classification"] == "sensitive")
    trace = service.trace_conclusion(actor_id="lic", material_id="m-photo")
    pinned_after = service.get_version("v1").snapshot["m-photo"]
    audit_ok, audit_count = service.verify_audit()

    return {
        "evidence_idempotent": (not first.replayed and replay.replayed
                                and first.resource_id == replay.resource_id),
        "pinned_basis_unchanged": pinned_before == pinned_after,
        "photo_conclusion_chain": [c.decision for c in service.list_conclusions("m-photo")],
        "story_conclusion_chain": [c.decision for c in service.list_conclusions("m-story")],
        "expired_material": sweep["expired"][0]["material_id"],
        "expired_impact_versions": [v["version_id"]
                                    for v in sweep["expired"][0]["impact"]["affected_versions"]],
        "expired_unsigned_deals": [d["deal_id"]
                                   for d in sweep["expired"][0]["impact"]["unsigned_deals"]],
        "deal_signing_blocked_during_freeze": sign_blocked,
        "radio_deal_accepted_by": signed.response["accepted_by_holders"],
        "judge_sensitive_redacted": judge_redacted,
        "trace_basis_keys": sorted(trace["chain"][0]["basis"].keys()),
        "pending_task_before_restart": "task-pending-restart",
        "audit_valid": audit_ok,
        "audit_events": audit_count,
    }


def run() -> dict[str, object]:
    """在临时文件库中执行完整场景，并在重启后核对队列与证据哈希。"""

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "rights_acceptance.sqlite3"
        database = Database(db_path, schema_extra=RIGHTS_SCHEMA)
        service = RightsService(database, CLOCK)
        result = _build(service)
        evidence_hash = service.get_evidence("ev-story", "lic")["sha256"]
        database.close()

        # 模拟服务中断恢复：重新打开同一文件库
        restarted = Database(db_path, schema_extra=RIGHTS_SCHEMA)
        service2 = RightsService(restarted, CLOCK)
        pending = [task.task_id for task in service2.list_pending_tasks()]
        hash_after_restart = service2.get_evidence("ev-story", "lic")["sha256"]
        chain_ok, chain_count = service2.verify_audit()
        restarted.close()

        result.update({
            "pending_queue_after_restart": pending,
            "queue_consistent_after_restart": "task-pending-restart" in pending,
            "evidence_hash_consistent_after_restart": evidence_hash == hash_after_restart,
            "audit_valid_after_restart": chain_ok,
            "audit_events_after_restart": chain_count,
        })
        result["status"] = "ok" if all([
            result["evidence_idempotent"], result["pinned_basis_unchanged"],
            result["photo_conclusion_chain"] == ["cleared", "restricted"],
            result["story_conclusion_chain"][-1] == "restricted",
            result["expired_material"] == "m-rubbing",
            "v1" in result["expired_impact_versions"],
            "deal-book" in result["expired_unsigned_deals"],
            "deal-radio" not in result["expired_unsigned_deals"],
            result["deal_signing_blocked_during_freeze"],
            set(result["radio_deal_accepted_by"]) == {"h-li", "h-zhang"},
            result["judge_sensitive_redacted"],
            result["queue_consistent_after_restart"],
            result["evidence_hash_consistent_after_restart"],
            result["audit_valid"], result["audit_valid_after_restart"],
        ]) else "failed"
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
