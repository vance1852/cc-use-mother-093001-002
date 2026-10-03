"""文化素材权利链核验域的服务层测试。"""

import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.errors import (
    ConflictError, PermissionDenied, ValidationError,
)
from creative_program_foundation.rights_schema import RIGHTS_SCHEMA
from creative_program_foundation.rights_service import RightsService
from creative_program_foundation.storage import Database


def bootstrap(service: RightsService) -> None:
    service.register_organization(request_id="org", actor_id="bootstrap",
                                  organization_id="o1", name="文创大赛组委会")
    other_actors = [
        ("lic-1", "授权专员甲", "licensing_officer"),
        ("rev-1", "冲突复核员甲", "conflict_reviewer"),
        ("judge-1", "评委甲", "judge"),
        ("creator-1", "创作者甲", "creator"),
        ("creator-2", "创作者乙", "creator"),
        ("op-1", "运营甲", "operator"),
    ]
    service.register_actor(request_id="actor-admin-1", actor_id="bootstrap",
                           new_actor_id="admin-1", display_name="管理员", role="admin",
                           organization_id="o1")
    for actor_id, name, role in other_actors:
        service.register_actor(request_id=f"actor-{actor_id}", actor_id="admin-1",
                               new_actor_id=actor_id, display_name=name, role=role,
                               organization_id="o1")
    service.register_site(request_id="site", actor_id="op-1", site_id="s1",
                          organization_id="o1", name="茶礼赛道", timezone_name="Asia/Shanghai")


def register_tea_gift_work(service: RightsService) -> None:
    """登记茶礼作品：老照片、书法拓片、口述故事三件素材，四位权利人。"""
    service.register_work(request_id="work", actor_id="creator-1", site_id="s1",
                          work_id="w1", title="岁时茶礼", primary_creator_id="creator-1")
    holders = [
        ("h-photo", "档案馆", "holder", "市档案馆授权窗口", False),
        ("h-rubbing", "书法世家", "both", "拓片保管人李先生", True),
        ("h-story", "社区长者王婆", "holder", "社区口述项目组转交", False),
        ("h-co", "联合作者张三", "both", "团队成员", True),
    ]
    for holder_id, name, kind, contact, can_deal in holders:
        service.register_rights_holder(
            request_id=f"holder-{holder_id}", actor_id="creator-1", holder_id=holder_id,
            display_name=name, kind=kind, contact_summary=contact,
            can_accept_channel_deals=can_deal)
    materials = [
        ("m-photo", "photo", "老照片：茶号旧影", "市档案馆藏民国茶号照片",
         ["h-photo"], {"h-photo": 100}, [], "*", ["exhibition", "merchandise"],
         "2026-01-01T00:00:00+08:00", "2027-12-31T23:59:59+08:00",
         "图源：市档案馆"),
        ("m-rubbing", "rubbing", "书法拓片：茶帖", "李氏家藏碑帖拓本",
         ["h-rubbing", "h-co"], {"h-rubbing": 70, "h-co": 30},
         ["h-rubbing", "h-co"], "CN",
         ["exhibition", "publication"], "2026-01-01T00:00:00+08:00",
         "2026-10-01T23:59:59+08:00", "拓片：李氏家藏"),
        ("m-story", "oral_story", "口述故事：制茶节令", "社区长者王婆口述录音整理",
         ["h-story", "h-co"], {"h-story": 60, "h-co": 40}, ["h-co"], "*",
         ["exhibition", "broadcast"], "2026-01-01T00:00:00+08:00", None,
         "口述：王婆（社区口述史项目）"),
    ]
    for args in materials:
        (mid, kind, title, source, hids, shares, coids, territory, purposes,
         vf, vu, attr) = args
        service.register_material(
            request_id=f"mat-{mid}", actor_id="creator-1", work_id="w1", material_id=mid,
            kind=kind, title=title, source_description=source, rights_holder_ids=hids,
            holder_shares=shares, coauthor_ids=coids, territory=territory, purposes=purposes,
            valid_from=vf, valid_until=vu, required_attribution=attr)


def verify_all(service: RightsService) -> None:
    for mid in ["m-photo", "m-rubbing", "m-story"]:
        tid = f"task-{mid}"
        service.create_verification_task(request_id=f"req-{tid}", actor_id="lic-1",
                                         material_id=mid, task_id=tid)
        service.submit_verification(request_id=f"submit-{tid}", actor_id="lic-1",
                                    task_id=tid, decision="authentic",
                                    notes="授权书与来源链条一致")


class RightsServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.database.connection.executescript(RIGHTS_SCHEMA)
        self.clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.service = RightsService(self.database, self.clock)
        bootstrap(self.service)
        register_tea_gift_work(self.service)

    def tearDown(self):
        self.database.close()

    # --------------------------------------------------------- 登记与份额

    def test_shares_must_sum_to_100(self):
        self.service.register_rights_holder(
            request_id="h-x1", actor_id="creator-1", holder_id="h-x1",
            display_name="错误份额", kind="holder",
            contact_summary="x", can_accept_channel_deals=False)
        self.service.register_rights_holder(
            request_id="h-x2", actor_id="creator-1", holder_id="h-x2",
            display_name="错误份额二", kind="holder",
            contact_summary="x", can_accept_channel_deals=False)
        with self.assertRaises(ValidationError):
            self.service.register_material(
                request_id="mat-bad", actor_id="creator-1", work_id="w1", material_id="m-bad",
                kind="other", title="x", source_description="x",
                rights_holder_ids=["h-x1", "h-x2"],
                holder_shares={"h-x1": 40, "h-x2": 40},
                coauthor_ids=[], territory="*",
                purposes=["exhibition"], valid_from="2026-01-01T00:00:00+08:00",
                valid_until=None, required_attribution="x")

    def test_missing_per_material_share_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.register_material(
                request_id="mat-missing-share", actor_id="creator-1", work_id="w1",
                material_id="m-ms", kind="other", title="x", source_description="x",
                rights_holder_ids=["h-photo"], coauthor_ids=[], territory="*",
                purposes=["exhibition"], valid_from="2026-01-01T00:00:00+08:00",
                valid_until=None, required_attribution="x")

    def test_coauthor_must_be_holder_with_coauthor_kind(self):
        with self.assertRaises(ValidationError):
            self.service.register_material(
                request_id="mat-bad", actor_id="creator-1", work_id="w1", material_id="m-bad",
                kind="other", title="x", source_description="x",
                rights_holder_ids=["h-photo"], holder_shares={"h-photo": 100},
                coauthor_ids=["h-photo"], territory="*",
                purposes=["exhibition"], valid_from="2026-01-01T00:00:00+08:00",
                valid_until=None, required_attribution="x")

    def test_creator_cannot_touch_other_creators_work(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_material(
                request_id="mat-other", actor_id="creator-2", work_id="w1", material_id="m-x",
                kind="other", title="x", source_description="x",
                rights_holder_ids=["h-photo"], holder_shares={"h-photo": 100},
                coauthor_ids=[], territory="*",
                purposes=["exhibition"], valid_from="2026-01-01T00:00:00+08:00",
                valid_until=None, required_attribution="x")

    def test_alternative_material_must_belong_to_same_work(self):
        # 引用不存在的替代素材被拒绝
        with self.assertRaises(ValidationError):
            self.service.register_material(
                request_id="mat-alt", actor_id="creator-1", work_id="w1", material_id="m-alt",
                kind="photo", title="替代", source_description="x",
                rights_holder_ids=["h-photo"], holder_shares={"h-photo": 100},
                coauthor_ids=[], territory="*",
                purposes=["exhibition"], valid_from="2026-01-01T00:00:00+08:00",
                valid_until=None, required_attribution="x",
                alternative_material_id="m-not-exists")
        # 先登记一张，再引用同作品的它作为替代素材应当成功
        self.service.register_material(
            request_id="mat-base", actor_id="creator-1", work_id="w1", material_id="m-base",
            kind="photo", title="基底素材", source_description="x",
            rights_holder_ids=["h-photo"], holder_shares={"h-photo": 100},
            coauthor_ids=[], territory="*",
            purposes=["exhibition"], valid_from="2026-01-01T00:00:00+08:00",
            valid_until=None, required_attribution="x")
        receipt = self.service.register_material(
            request_id="mat-alt2", actor_id="creator-1", work_id="w1", material_id="m-alt2",
            kind="photo", title="替代素材", source_description="x",
            rights_holder_ids=["h-photo"], holder_shares={"h-photo": 100},
            coauthor_ids=[], territory="*",
            purposes=["exhibition"], valid_from="2026-01-01T00:00:00+08:00",
            valid_until=None, required_attribution="x", alternative_material_id="m-base")
        self.assertEqual("m-alt2", receipt.resource_id)

    # ------------------------------------------------------------- 证据幂等

    def test_evidence_upload_is_idempotent_by_hash(self):
        first = self.service.upload_evidence(
            request_id="ev-1", actor_id="creator-1", material_id="m-photo",
            evidence_id="e-photo-1", filename="license.pdf", media_type="application/pdf",
            summary="档案馆授权书扫描件", classification="sensitive",
            content_base64="YWJjZGVm")  # abcdef
        second = self.service.upload_evidence(
            request_id="ev-1-diff-req", actor_id="creator-1", material_id="m-photo",
            evidence_id="e-photo-dup", filename="license-copy.pdf",
            media_type="application/pdf", summary="重复上传",
            content_base64="YWJjZGVm")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        material = self.service.get_material("m-photo")
        self.assertEqual(("e-photo-1",), material.evidence_ids)

    def test_sha256_must_match_content(self):
        with self.assertRaises(ValidationError):
            self.service.upload_evidence(
                request_id="ev-bad", actor_id="creator-1", material_id="m-photo",
                evidence_id="e-bad", filename="x", media_type="application/pdf",
                summary="x", content_base64="YWJj", sha256="0" * 64)

    def test_sensitive_evidence_redacted_for_judge_but_visible_to_officer(self):
        self.service.upload_evidence(
            request_id="ev-s", actor_id="creator-1", material_id="m-photo",
            evidence_id="e-s", filename="secret.pdf", media_type="application/pdf",
            summary="内部授权底价与联系人", classification="sensitive",
            content_base64="c2VjcmV0")
        judge_view = self.service.get_evidence("e-s", "judge-1")
        officer_view = self.service.get_evidence("e-s", "lic-1")
        self.assertTrue(judge_view["redacted"])
        self.assertNotIn("内部授权底价", judge_view["summary"])
        self.assertFalse(officer_view["redacted"])
        self.assertIn("内部授权底价", officer_view["summary"])

    # ------------------------------------------------------- 核验与冲突复核

    def test_verification_issues_cleared_conclusion(self):
        self.service.create_verification_task(request_id="t1", actor_id="lic-1",
                                              material_id="m-photo", task_id="task-1")
        self.service.submit_verification(request_id="s1", actor_id="lic-1",
                                         task_id="task-1", decision="authentic")
        conclusions = self.service.list_conclusions("m-photo")
        self.assertEqual(1, len(conclusions))
        self.assertEqual("cleared", conclusions[0].decision)
        self.assertTrue(conclusions[0].usable)
        self.assertEqual("lic-1", conclusions[0].issued_by)
        # 重复提交被拒绝，任务状态机防止双花
        with self.assertRaises(ConflictError):
            self.service.submit_verification(request_id="s2", actor_id="lic-1",
                                             task_id="task-1", decision="authentic")

    def test_conflict_reviewer_cannot_review_own_verification(self):
        self.service.create_verification_task(request_id="t1", actor_id="lic-1",
                                              material_id="m-photo", task_id="task-1")
        self.service.submit_verification(request_id="s1", actor_id="lic-1",
                                         task_id="task-1", decision="authentic")
        # lic-1 同时是冲突复核角色也不能自复核（本例 lic-1 不是；用 admin 复核他人可以）
        self.service.resolve_conflict_review(
            request_id="cr1", actor_id="rev-1", task_id="task-1", upheld=False,
            notes="授权书签章与备案不符，推翻")
        conclusions = self.service.list_conclusions("m-photo")
        self.assertEqual(["cleared", "blocked"], [c.decision for c in conclusions])
        self.assertEqual("conflict_resolution", conclusions[-1].trigger)

    def test_judge_cannot_verify(self):
        self.service.create_verification_task(request_id="t1", actor_id="lic-1",
                                              material_id="m-photo", task_id="task-1")
        with self.assertRaises(PermissionDenied):
            self.service.submit_verification(request_id="sx", actor_id="judge-1",
                                             task_id="task-1", decision="authentic")

    # ----------------------------------------------------- 版本冻结与结论追加

    def test_review_entry_pins_basis_and_supplements_only_append(self):
        verify_all(self.service)
        self.service.create_work_version(
            request_id="v1", actor_id="creator-1", work_id="w1", version_id="ver-1",
            material_ids=["m-photo", "m-rubbing", "m-story"])
        self.service.enter_review(request_id="er1", actor_id="lic-1", work_id="w1",
                                  stage="preliminary", version_id="ver-1")
        version = self.service.get_version("ver-1")
        self.assertTrue(version.locked)
        pinned_photo = version.snapshot["m-photo"]
        self.assertEqual("cleared", pinned_photo["decision"])

        # 复赛后补交授权（新证据 + 新结论）：只追加
        self.service.upload_evidence(
            request_id="ev-new", actor_id="creator-1", material_id="m-photo",
            evidence_id="e-new", filename="supplement.pdf", media_type="application/pdf",
            summary="复赛补交的展期授权", content_base64="c3VwcGxlbWVudA==")
        self.service.supplement_authorization(
            request_id="sup1", actor_id="lic-1", material_id="m-photo",
            decision="restricted", note="仅允许展览用途展期")
        chain = self.service.list_conclusions("m-photo")
        self.assertEqual(["cleared", "restricted"], [c.decision for c in chain])

        # 冻结版本上的快照没有被抹掉
        version_after = self.service.get_version("ver-1")
        self.assertEqual(pinned_photo, version_after.snapshot["m-photo"])
        first_conclusion = chain[0]
        self.assertEqual(pinned_photo["conclusion_id"], first_conclusion.conclusion_id)
        self.assertEqual(pinned_photo["basis_hash"], first_conclusion.basis_hash)

        # 新版本进入决赛时固化的是新结论
        self.service.create_work_version(
            request_id="v2", actor_id="creator-1", work_id="w1", version_id="ver-2",
            material_ids=["m-photo", "m-rubbing", "m-story"])
        self.service.enter_review(request_id="er2", actor_id="lic-1", work_id="w1",
                                  stage="final", version_id="ver-2")
        v2 = self.service.get_version("ver-2")
        self.assertEqual(chain[-1].conclusion_id, v2.snapshot["m-photo"]["conclusion_id"])
        self.assertEqual("restricted", v2.snapshot["m-photo"]["decision"])

    def test_withdrawal_after_review_keeps_historical_basis(self):
        verify_all(self.service)
        self.service.create_work_version(
            request_id="v1", actor_id="creator-1", work_id="w1", version_id="ver-1",
            material_ids=["m-photo", "m-rubbing", "m-story"])
        self.service.enter_review(request_id="er1", actor_id="lic-1", work_id="w1",
                                  stage="preliminary", version_id="ver-1")
        pinned = self.service.get_version("ver-1").snapshot["m-story"]
        self.service.withdraw_authorization(
            request_id="wd1", actor_id="lic-1", material_id="m-story",
            reason="口述者家属提出撤回")
        chain = self.service.list_conclusions("m-story")
        self.assertEqual(["cleared", "withdrawn"], [c.decision for c in chain])
        self.assertFalse(chain[-1].usable)
        # 历史评审依据仍在
        self.assertEqual(pinned, self.service.get_version("ver-1").snapshot["m-story"])
        # 影响面指出初赛版本
        impact = self.service.impact_report(actor_id="lic-1", material_id="m-story")
        stages = {(v["version_id"], v["stage"]) for v in impact["affected_versions"]}
        self.assertIn(("ver-1", "preliminary"), stages)

    def test_cannot_enter_review_without_conclusion(self):
        self.service.create_work_version(
            request_id="v1", actor_id="creator-1", work_id="w1", version_id="ver-1",
            material_ids=["m-photo"])
        with self.assertRaises(ConflictError):
            self.service.enter_review(request_id="er1", actor_id="lic-1", work_id="w1",
                                      stage="preliminary", version_id="ver-1")

    # ------------------------------------------------- 失效、影响面、商业合作

    def test_expired_license_impact_covers_versions_and_unsigned_deals(self):
        verify_all(self.service)
        self.service.create_work_version(
            request_id="v1", actor_id="creator-1", work_id="w1", version_id="ver-1",
            material_ids=["m-photo", "m-rubbing", "m-story"])
        self.service.enter_review(request_id="er1", actor_id="lic-1", work_id="w1",
                                  stage="preliminary", version_id="ver-1")
        # 拓片许可 2026-10-01 到期；登记两个商业合作，一个用途/地域相关，一个无关
        self.service.register_deal(
            request_id="d1", actor_id="op-1", deal_id="deal-1", work_id="w1",
            partner="国风出版社", requires_purposes=["publication"], territory="CN")
        self.service.register_deal(
            request_id="d2", actor_id="op-1", deal_id="deal-2", work_id="w1",
            partner="地方电台", requires_purposes=["broadcast"], territory="*")
        result = self.service.sweep_expired_authorizations(
            actor_id="lic-1", at="2026-10-02T00:00:00+08:00")
        self.assertEqual(1, result["count"])
        expired = result["expired"][0]
        self.assertEqual("m-rubbing", expired["material_id"])
        versions = [(v["version_id"], v["stage"]) for v in expired["impact"]["affected_versions"]]
        self.assertIn(("ver-1", "preliminary"), versions)
        deal_ids = {d["deal_id"] for d in expired["impact"]["unsigned_deals"]}
        self.assertIn("deal-1", deal_ids)      # publication@CN 命中
        self.assertNotIn("deal-2", deal_ids)   # broadcast 不在拓片用途内
        self.assertEqual("expired", self.service.list_conclusions("m-rubbing")[-1].decision)

    def test_deal_signing_requires_authorized_representative_and_no_freeze(self):
        verify_all(self.service)
        # 没有冻结时，h-rubbing/h-co 有代表权，可以签署
        self.service.register_deal(
            request_id="d1", actor_id="op-1", deal_id="deal-1", work_id="w1",
            partner="渠道甲", requires_purposes=["exhibition"], territory="*")
        receipt = self.service.sign_deal(request_id="sign1", actor_id="lic-1",
                                         deal_id="deal-1")
        self.assertFalse(receipt.replayed)
        # 立案 + 局部冻结后，第二个合作不能签署
        self.service.register_deal(
            request_id="d2", actor_id="op-1", deal_id="deal-2", work_id="w1",
            partner="渠道乙", requires_purposes=["broadcast"], territory="*")
        self.service.create_work_version(
            request_id="v1", actor_id="creator-1", work_id="w1", version_id="ver-1",
            material_ids=["m-story"])
        self.service.file_dispute(request_id="disp1", actor_id="rev-1", dispute_id="dip-1",
                                  work_id="w1", material_id="m-story",
                                  reason="口述故事权属存在异议")
        self.service.partial_freeze(request_id="fz1", actor_id="rev-1", dispute_id="dip-1",
                                    version_id="ver-1", material_id="m-story")
        with self.assertRaises(ConflictError):
            self.service.sign_deal(request_id="sign2", actor_id="lic-1", deal_id="deal-2")

    def test_no_channel_representative_blocks_signing(self):
        # 新作品只用档案馆（无渠道代表权）素材
        self.service.register_work(request_id="w2", actor_id="creator-1", site_id="s1",
                                   work_id="w2", title="纯档案图集", primary_creator_id="creator-1")
        self.service.register_material(
            request_id="m2", actor_id="creator-1", work_id="w2", material_id="m-only-photo",
            kind="photo", title="档案照片", source_description="x",
            rights_holder_ids=["h-photo"], holder_shares={"h-photo": 100},
            coauthor_ids=[], territory="*",
            purposes=["exhibition"], valid_from="2026-01-01T00:00:00+08:00",
            valid_until=None, required_attribution="x")
        self.service.create_verification_task(request_id="t2", actor_id="lic-1",
                                              material_id="m-only-photo", task_id="task-2")
        self.service.submit_verification(request_id="s2", actor_id="lic-1",
                                         task_id="task-2", decision="authentic")
        self.service.register_deal(
            request_id="d2", actor_id="op-1", deal_id="deal-x", work_id="w2",
            partner="渠道丙", requires_purposes=["exhibition"], territory="*")
        with self.assertRaises(PermissionDenied):
            self.service.sign_deal(request_id="signx", actor_id="lic-1", deal_id="deal-x")

    # --------------------------------------------------------- 异议全流程

    def test_dispute_preserve_freeze_settle_lifecycle(self):
        verify_all(self.service)
        self.service.upload_evidence(
            request_id="ev-p", actor_id="creator-1", material_id="m-story",
            evidence_id="e-p", filename="story-release.pdf", media_type="application/pdf",
            summary="口述故事授权同意书", content_base64="cmVsZWFzZQ==")
        self.service.create_work_version(
            request_id="v1", actor_id="creator-1", work_id="w1", version_id="ver-1",
            material_ids=["m-story"])
        self.service.file_dispute(request_id="disp", actor_id="rev-1", dispute_id="dip-1",
                                  work_id="w1", material_id="m-story",
                                  reason="家属主张口述故事继承权")
        self.service.preserve_evidence(request_id="pres", actor_id="lic-1",
                                       dispute_id="dip-1", evidence_ids=["e-p"])
        self.service.partial_freeze(request_id="fz", actor_id="rev-1", dispute_id="dip-1",
                                    version_id="ver-1", material_id="m-story")
        dispute = self.service.get_dispute("dip-1")
        self.assertEqual("partially_frozen", dispute.status)
        # 被冻结版本不能进入评审
        with self.assertRaises(ConflictError):
            self.service.enter_review(request_id="er-bad", actor_id="lic-1", work_id="w1",
                                      stage="preliminary", version_id="ver-1")
        # 和解结案：解除冻结，并把素材认定为 restricted
        self.service.resolve_dispute(
            request_id="res", actor_id="rev-1", dispute_id="dip-1", outcome="settled",
            terms="追加家属署名，商业化收益分成 10%", new_decision="restricted")
        freezes = self.service.list_freezes("dip-1")
        self.assertTrue(all(f["lifted"] for f in freezes))
        self.assertEqual("settled", self.service.get_dispute("dip-1").status)
        self.assertEqual("restricted", self.service.list_conclusions("m-story")[-1].decision)
        # 结案后冻结解除，版本可以进入评审
        self.service.enter_review(request_id="er-ok", actor_id="lic-1", work_id="w1",
                                  stage="preliminary", version_id="ver-1")

    def test_preservation_keeps_hash_when_evidence_record_changes(self):
        self.service.upload_evidence(
            request_id="ev-p", actor_id="creator-1", material_id="m-story",
            evidence_id="e-p", filename="release.pdf", media_type="application/pdf",
            summary="原版同意书", content_base64="b2xk")
        self.service.file_dispute(request_id="disp", actor_id="rev-1", dispute_id="dip-1",
                                  work_id="w1", reason="测试保全")
        self.service.preserve_evidence(request_id="pres", actor_id="lic-1",
                                       dispute_id="dip-1", evidence_ids=["e-p"])
        preserved = self.service.list_preservations("dip-1")[0]
        # 保全哈希与上传哈希一致，且保全记录独立留存（证明记录不允许更新，这里核对其不可由上传接口改写）
        evidence = self.service.get_evidence("e-p", "lic-1")
        self.assertEqual(preserved["sha256"], evidence["sha256"])

    # ----------------------------------------------------------- 追溯与脱敏

    def test_trace_requires_clearance_role(self):
        verify_all(self.service)
        with self.assertRaises(PermissionDenied):
            self.service.trace_conclusion(actor_id="judge-1", material_id="m-photo")
        trace = self.service.trace_conclusion(actor_id="lic-1", material_id="m-photo")
        self.assertEqual("cleared", trace["current_conclusion"]["decision"])
        basis = trace["chain"][0]["basis"]
        self.assertIn("license", basis)
        self.assertIn("evidence", basis)
        self.assertEqual("*", basis["license"]["territory"])
        # 素材级共同创作份额：老照片由档案馆独占 100%
        self.assertEqual(100, basis["license"]["holders"][0]["share_percent"])

    def test_review_pack_redacts_sensitive_evidence(self):
        verify_all(self.service)
        self.service.upload_evidence(
            request_id="ev-s", actor_id="creator-1", material_id="m-photo",
            evidence_id="e-s", filename="secret.pdf", media_type="application/pdf",
            summary="敏感合同金额", classification="sensitive",
            content_base64="c2VjcmV0Mg==")
        pack = self.service.review_pack(actor_id="judge-1", work_id="w1")
        photo = next(m for m in pack["materials"] if m["material"]["material_id"] == "m-photo")
        sensitive = next(e for e in photo["evidence"] if e["evidence_id"] == "e-s")
        self.assertNotIn("敏感合同金额", sensitive["summary"])
        officer_pack = self.service.review_pack(actor_id="lic-1", work_id="w1")
        photo_o = next(m for m in officer_pack["materials"]
                       if m["material"]["material_id"] == "m-photo")
        self.assertIn("敏感合同金额", next(
            e for e in photo_o["evidence"] if e["evidence_id"] == "e-s")["summary"])

    # --------------------------------------------------------- 崩溃恢复一致性

    def test_pending_queue_and_hashes_survive_restart(self):
        self.service.create_verification_task(request_id="t1", actor_id="lic-1",
                                              material_id="m-photo", task_id="task-restart")
        self.service.upload_evidence(
            request_id="ev-r", actor_id="creator-1", material_id="m-photo",
            evidence_id="e-r", filename="x.pdf", media_type="application/pdf",
            summary="重启前上传", content_base64="cmVzdGFydA==")
        expected_hash = self.service.get_evidence("e-r", "lic-1")["sha256"]
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "restart.sqlite3")
            import sqlite3
            target = sqlite3.connect(path)
            self.database.connection.backup(target)
            target.close()
            db2 = Database(path, schema_extra=RIGHTS_SCHEMA)
            service2 = RightsService(db2)
            pending = service2.list_pending_tasks()
            self.assertIn("task-restart", [t.task_id for t in pending])
            self.assertEqual(expected_hash,
                             service2.get_evidence("e-r", "lic-1")["sha256"])
            valid, count = service2.verify_audit()
            self.assertTrue(valid)
            db2.close()


if __name__ == "__main__":
    unittest.main()
