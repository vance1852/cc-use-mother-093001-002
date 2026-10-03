"""权利链核验的离线端到端验收。

复现大赛场景：茶礼作品同时引用老照片、书法拓片和社区口述故事，
完成登记、双职责核验、进入复赛评审、异议处理、许可撤回与补交、
渠道合作签署、敏感证明分级读取，最后模拟服务中断恢复并校验一致性。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.service import DomainService

from .service import RightsChainService
from .storage import RightsDatabase

CLOCK = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))


def _bootstrap(database: RightsDatabase) -> tuple[DomainService, RightsChainService]:
    foundation = DomainService(database, CLOCK)
    foundation.register_organization(request_id="req-org", actor_id="bootstrap",
                                     organization_id="org-contest", name="文化创意大赛组委会")
    foundation.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-1",
                              display_name="系统管理员", role="admin", organization_id="org-contest")
    actors = [
        ("operator-1", "赛事运营", "operator"),
        ("creator-tea", "投稿人阿茶", "creator"),
        ("coauthor-lin", "联合作者小林", "creator"),
        ("verifier-1", "真实性核验员", "verifier"),
        ("reviewer-1", "冲突复核员", "reviewer"),
        ("judge-1", "复赛评委", "judge"),
        ("officer-1", "授权专员", "licensing_officer"),
        ("case-1", "异议处理员", "case_handler"),
    ]
    for actor_id, name, role in actors:
        foundation.register_actor(request_id=f"req-actor-{actor_id}", actor_id="admin-1",
                                  new_actor_id=actor_id, display_name=name,
                                  role=role, organization_id="org-contest")
    foundation.register_site(request_id="req-site", actor_id="operator-1", site_id="site-semi",
                             organization_id="org-contest", name="复赛评审节点",
                             timezone_name="Asia/Shanghai")
    return foundation, RightsChainService(database, CLOCK)


def _register_materials(service: RightsChainService) -> dict[str, str]:
    material_ids = {}
    specs = [
        ("photo", "req-mat-photo", "清末茶山老照片", "老照片", "市档案馆馆藏清末茶山照片",
         [{"name": "市档案馆", "share_percent": 100}],
         {"territory": "中国大陆", "usage_scope": "展览与文创衍生",
          "valid_from": "2026-01-01", "valid_until": "2027-12-31",
          "required_attribution": "图片来源：市档案馆"},
         [{"content": "档案馆授权邮件：许可范围展览与文创衍生，商业化期限至2027年底，须署名。",
           "summary": "档案馆授权邮件（含许可范围与商业化期限）", "sensitive": True}],
         [{"note": "可替换为公有领域同期茶山照片"}]),
        ("rubbing", "req-mat-rubbing", "明代茶经书法拓片", "书法拓片", "书法家后人收藏明代拓片",
         [{"name": "书法家后人张某", "share_percent": 100}],
         {"territory": "全球", "usage_scope": "展览",
          "valid_from": "2026-01-01", "valid_until": "2026-12-31",
          "required_attribution": "拓片收藏：张某"},
         [{"content": "张某授权邮件：仅限展览用途，须署名收藏者。",
           "summary": "张某授权邮件（限展览用途）", "sensitive": True}],
         [{"note": "可替换为公版碑帖拓片"}]),
        ("oral", "req-mat-oral", "社区茶农口述故事", "社区口述", "社区茶农口述采茶往事",
         [{"name": "社区居民代表", "share_percent": 60},
          {"name": "记录整理人", "share_percent": 40}],
         {"territory": "中国大陆", "usage_scope": "展览与出版物",
          "valid_from": "2026-01-01", "valid_until": None,
          "required_attribution": "口述：社区茶农"},
         [{"content": "社区知情同意书扫描件：同意展览与出版使用。",
           "summary": "社区知情同意书", "sensitive": True}],
         []),
    ]
    for key, request_id, title, source_type, description, holders, license_spec, evidence, alternatives in specs:
        result = service.register_material(
            request_id=request_id, actor_id="creator-tea", site_id="site-semi",
            work_id="work-tea-gift", title=title, source_type=source_type,
            source_description=description, rights_holders=holders,
            license=license_spec, alternatives=alternatives, evidence=evidence)
        material_ids[key] = result["material_id"]
    return material_ids


def _verify_all(service: RightsChainService) -> None:
    pending = service.list_verification_tasks("verifier-1")
    for index, task in enumerate(pending):
        service.decide_verification(request_id=f"req-verify-{index}", actor_id="verifier-1",
                                    task_id=task["task_id"], decision="approved",
                                    note="来源与证明一致")
    pending = service.list_verification_tasks("reviewer-1")
    for index, task in enumerate(pending):
        service.decide_verification(request_id=f"req-review-{index}", actor_id="reviewer-1",
                                    task_id=task["task_id"], decision="approved",
                                    note="许可范围互不冲突")


def run() -> dict[str, object]:
    """执行完整权利链场景并返回检查结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "rights.sqlite3"
        database = RightsDatabase(path)
        foundation, service = _bootstrap(database)

        # 作品与联合创作团队；指定团队代表以回应代表权争议。
        service.register_work(request_id="req-work", actor_id="creator-tea", site_id="site-semi",
                              work_id="work-tea-gift", title="茶礼《山间茶事》",
                              team=[{"member_id": "creator-tea", "share_percent": 60},
                                    {"member_id": "coauthor-lin", "share_percent": 40}])
        service.set_team_representative(request_id="req-rep", actor_id="operator-1",
                                        work_id="work-tea-gift", member_id="creator-tea")

        # 登记三类素材并完成双职责核验。
        materials = _register_materials(service)
        _verify_all(service)

        # 版本进入复赛评审，生成第一份权利结论。
        version = service.create_version(request_id="req-version", actor_id="creator-tea",
                                         work_id="work-tea-gift", version_no=1,
                                         material_ids=list(materials.values()))
        version_id = version["version_id"]
        entered = service.enter_review(request_id="req-enter", actor_id="operator-1",
                                       version_id=version_id, stage="semi_final")
        first_conclusion = entered["conclusion"]["conclusion_id"]

        # 登记一项尚未签署的渠道合作。
        collaboration = service.create_collaboration(request_id="req-collab", actor_id="officer-1",
                                                     version_id=version_id, partner="文创渠道商A",
                                                     channel="电商联名")

        # 异议：立案 → 证据保全 → 局部冻结 → 和解。
        objection = service.file_objection(request_id="req-objection", actor_id="coauthor-lin",
                                           target_type="material", target_id=materials["rubbing"],
                                           reason="对拓片许可范围与署名方式有异议")
        objection_id = objection["objection_id"]
        preserved = service.preserve_evidence(request_id="req-preserve", actor_id="case-1",
                                              objection_id=objection_id)
        frozen = service.freeze_targets(request_id="req-freeze", actor_id="case-1",
                                        objection_id=objection_id,
                                        targets=[{"target_type": "material",
                                                  "target_id": materials["rubbing"],
                                                  "scope_note": "冻结拓片素材的商业化使用"}])
        frozen_unusable = not frozen["refreshed_versions"][0]["usable"]
        settled = service.settle_objection(request_id="req-settle", actor_id="case-1",
                                           objection_id=objection_id,
                                           resolution="双方和解：补充署名并限定用途")
        settled_usable = settled["refreshed_versions"][0]["usable"]

        # 撤回拓片许可：影响报告应指出复赛中的版本与未签署合作。
        rubbing_detail = service.get_material("officer-1", materials["rubbing"])
        rubbing_license = rubbing_detail["licenses"][0]["license_id"]
        revoked = service.revoke_license(request_id="req-revoke", actor_id="officer-1",
                                         license_id=rubbing_license,
                                         reason="授权人书面撤回商业化授权")
        impact = revoked["impact"]
        impact_hit = (
            any(item["version_id"] == version_id and item["stage"] == "semi_final"
                for item in impact["affected_versions"])
            and any(item["collaboration_id"] == collaboration["collaboration_id"]
                    for item in impact["unsigned_collaborations"]))

        # 补交新授权后生成新结论恢复可用；历史结论依据保持不变。
        service.submit_license(request_id="req-license-2", actor_id="creator-tea",
                               material_id=materials["rubbing"], territory="全球",
                               usage_scope="展览与文创衍生", valid_from="2026-10-01",
                               valid_until="2028-12-31",
                               required_attribution="拓片收藏：张某")
        conclusions = service.list_conclusions("officer-1", version_id)
        current = conclusions[-1]
        old = service.get_conclusion("officer-1", first_conclusion)
        old_rubbing = next(item for item in old["basis"]["materials"]
                           if item["material_id"] == materials["rubbing"])
        old_basis_kept = (
            old["standing"] == "superseded"
            and old_rubbing["licenses"][0]["status"] == "active")

        # 团队代表签署渠道合作。
        signed = service.sign_collaboration(request_id="req-sign", actor_id="creator-tea",
                                            collaboration_id=collaboration["collaboration_id"])

        # 隐私：评委读取敏感证明被脱敏，授权专员可追溯完整依据。
        judge_view = service.get_material("judge-1", materials["photo"])
        judge_redacted = judge_view["evidence"][0]["summary"] == "[受限]"
        trace = service.trace_conclusion_basis("officer-1", current["conclusion_id"])
        trace_complete = (trace["basis_hash_valid"]
                          and any(item["summary"] != "[受限]" for item in trace["evidence"]))

        # 幂等：相同证据重复上传得到同一记录。
        first_upload = service.upload_evidence(request_id="req-ev-1", actor_id="creator-tea",
                                               material_id=materials["oral"],
                                               content="口述授权录音文字稿第一版",
                                               summary="口述授权录音文字稿", sensitive=True)
        second_upload = service.upload_evidence(request_id="req-ev-2", actor_id="creator-tea",
                                                material_id=materials["oral"],
                                                content="口述授权录音文字稿第一版",
                                                summary="口述授权录音文字稿", sensitive=True)
        evidence_deduplicated = (second_upload["deduplicated"]
                                 and first_upload["evidence_id"] == second_upload["evidence_id"])

        # 留下一个待核验素材，模拟服务中断后恢复。
        service.register_material(request_id="req-mat-music", actor_id="creator-tea",
                                  site_id="site-semi", work_id="work-tea-gift",
                                  title="备用山歌录音", source_type="音乐",
                                  source_description="采风录制山歌",
                                  rights_holders=[{"name": "采风团队", "share_percent": 100}],
                                  license={"territory": "中国大陆", "usage_scope": "展览",
                                           "valid_from": "2026-01-01", "valid_until": None,
                                           "required_attribution": "演唱：村民"},
                                  evidence=[])
        queue_before = service.list_verification_tasks("verifier-1")
        database.close()

        recovered = RightsDatabase(path)
        recovered_service = RightsChainService(recovered, CLOCK)
        queue_after = recovered_service.list_verification_tasks("verifier-1")
        recovery = recovered_service.verify_recovery("admin-1")
        queue_consistent = [task["task_id"] for task in queue_before] == \
            [task["task_id"] for task in queue_after]
        recovered.close()

        result = {
            "status": "ok",
            "conclusions": len(conclusions),
            "current_usable": current["usable"],
            "frozen_unusable": frozen_unusable,
            "settled_usable": settled_usable,
            "impact_hit": impact_hit,
            "old_basis_kept": old_basis_kept,
            "signed": signed["status"] == "signed",
            "judge_redacted": judge_redacted,
            "trace_complete": trace_complete,
            "evidence_deduplicated": evidence_deduplicated,
            "preserved_count": preserved["evidence_count"],
            "queue_consistent": queue_consistent,
            "pending_after_recovery": recovery["pending_tasks"],
            "recovery_consistent": recovery["consistent"],
        }
        result["ok"] = all([
            result["current_usable"], frozen_unusable, settled_usable, impact_hit,
            old_basis_kept, result["signed"], judge_redacted, trace_complete,
            evidence_deduplicated, queue_consistent,
            recovery["pending_tasks"] == 2, recovery["consistent"],
        ])
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
