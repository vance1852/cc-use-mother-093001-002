import unittest
from datetime import datetime, timezone

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.errors import ConflictError, PermissionDenied, ValidationError
from creative_program_foundation.service import DomainService
from rights_chain.service import RightsChainService
from rights_chain.storage import RightsDatabase

CLOCK = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))


class RightsChainTestBase(unittest.TestCase):
    def setUp(self):
        self.database = RightsDatabase()
        self.foundation = DomainService(self.database, CLOCK)
        self.service = RightsChainService(self.database, CLOCK)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="大赛组委会")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                       display_name="管理员", role="admin", organization_id="o1")
        for actor_id, role in [("op1", "operator"), ("c1", "creator"), ("c2", "creator"),
                               ("v1", "verifier"), ("r1", "reviewer"), ("j1", "judge"),
                               ("o1f", "licensing_officer"), ("h1", "case_handler")]:
            self.foundation.register_actor(request_id=f"actor-{actor_id}", actor_id="a1",
                                           new_actor_id=actor_id, display_name=actor_id,
                                           role=role, organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="op1", site_id="s1",
                                      organization_id="o1", name="复赛节点",
                                      timezone_name="Asia/Shanghai")
        self.service.register_work(request_id="work", actor_id="c1", site_id="s1",
                                   work_id="w1", title="茶礼",
                                   team=[{"member_id": "c1", "share_percent": 60},
                                         {"member_id": "c2", "share_percent": 40}])

    def tearDown(self):
        self.database.close()

    def _material(self, request_id="mat", work_id="w1", actor="c1", **overrides):
        params = {
            "request_id": request_id, "actor_id": actor, "site_id": "s1", "work_id": work_id,
            "title": "老照片", "source_type": "老照片",
            "source_description": "市档案馆馆藏照片",
            "rights_holders": [{"name": "市档案馆", "share_percent": 100}],
            "license": {"territory": "中国大陆", "usage_scope": "展览",
                        "valid_from": "2026-01-01", "valid_until": "2027-12-31",
                        "required_attribution": "图片来源：市档案馆"},
            "evidence": [{"content": "授权邮件原文", "summary": "授权邮件", "sensitive": True}],
        }
        params.update(overrides)
        return self.service.register_material(**params)

    def _verified_material(self, request_id="mat", **overrides):
        result = self._material(request_id=request_id, **overrides)
        self._verify(result["verification_task_ids"])
        return result

    def _verify(self, task_ids):
        for index, task_id in enumerate(task_ids):
            actor = "v1" if index == 0 else "r1"
            self.service.decide_verification(request_id=f"decide-{task_id}", actor_id=actor,
                                             task_id=task_id, decision="approved")

    def _version_in_review(self, stage="semi_final"):
        materials = [self._verified_material(request_id=f"mat-{key}", title=title,
                                             source_description=f"来源{key}")
                     for key, title in [("a", "老照片"), ("b", "书法拓片"), ("c", "口述故事")]]
        material_ids = [item["material_id"] for item in materials]
        license_ids = [item["license_id"] for item in materials]
        version = self.service.create_version(request_id="version", actor_id="c1", work_id="w1",
                                              version_no=1, material_ids=material_ids)
        entered = self.service.enter_review(request_id="enter", actor_id="op1",
                                            version_id=version["version_id"], stage=stage)
        return version["version_id"], material_ids, license_ids, entered["conclusion"]


class MaterialRegistrationTest(RightsChainTestBase):
    def test_registration_creates_dual_verification_tasks(self):
        result = self._material()
        material = self.service.get_material("a1", result["material_id"])
        self.assertEqual("pending_verification", material["status"])
        self.assertEqual(2, len(result["verification_task_ids"]))
        queue = self.service.list_verification_tasks("v1")
        self.assertEqual(1, len(queue))
        self.assertEqual("authenticity", queue[0]["task_type"])
        queue = self.service.list_verification_tasks("r1")
        self.assertEqual("conflict_review", queue[0]["task_type"])

    def test_registration_records_holders_license_alternatives(self):
        substitute = self._material(request_id="mat-sub", title="公有领域照片")
        result = self._material(
            request_id="mat-main",
            rights_holders=[{"name": "档案馆", "share_percent": 70},
                            {"name": "修复师", "share_percent": 30}],
            alternatives=[{"material_id": substitute["material_id"], "note": "首选替代"},
                          {"note": "或改用自行拍摄照片"}])
        material = self.service.get_material("a1", result["material_id"])
        self.assertEqual(2, len(material["holders"]))
        self.assertEqual("展览", material["licenses"][0]["usage_scope"])
        self.assertEqual("图片来源：市档案馆", material["licenses"][0]["required_attribution"])
        self.assertEqual(2, len(material["alternatives"]))
        self.assertEqual(substitute["material_id"], material["alternatives"][0]["alternative_material_id"])

    def test_holders_shares_must_sum_to_100(self):
        with self.assertRaises(ValidationError):
            self._material(rights_holders=[{"name": "甲", "share_percent": 50}])

    def test_license_term_must_be_ordered(self):
        with self.assertRaises(ValidationError):
            self._material(license={"territory": "中国大陆", "usage_scope": "展览",
                                    "valid_from": "2027-01-01", "valid_until": "2026-01-01",
                                    "required_attribution": "署名"})

    def test_same_request_id_replays_registration(self):
        first = self._material()
        second = self._material()
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["material_id"], second["material_id"])

    def test_judge_cannot_register_material(self):
        with self.assertRaises(PermissionDenied):
            self._material(actor="j1")


class VerificationTest(RightsChainTestBase):
    def test_dual_approval_marks_material_verified(self):
        result = self._material()
        authenticity, conflict = result["verification_task_ids"]
        self.service.decide_verification(request_id="d1", actor_id="v1",
                                         task_id=authenticity, decision="approved")
        final = self.service.decide_verification(request_id="d2", actor_id="r1",
                                                 task_id=conflict, decision="approved")
        self.assertEqual("verified", final["material_status"])

    def test_wrong_role_cannot_decide(self):
        result = self._material()
        authenticity, _ = result["verification_task_ids"]
        with self.assertRaises(PermissionDenied):
            self.service.decide_verification(request_id="d1", actor_id="r1",
                                             task_id=authenticity, decision="approved")

    def test_registrant_cannot_verify_own_material(self):
        result = self._material()
        authenticity, _ = result["verification_task_ids"]
        with self.assertRaises(PermissionDenied):
            self.service.decide_verification(request_id="d1", actor_id="c1",
                                             task_id=authenticity, decision="approved")

    def test_duties_must_be_separate_people(self):
        result = self._material()
        authenticity, conflict = result["verification_task_ids"]
        self.service.decide_verification(request_id="d1", actor_id="v1",
                                         task_id=authenticity, decision="approved")
        # 完成真实性核验的人不能再以另一职责复核同一素材。
        with self.assertRaises(PermissionDenied):
            self.service.decide_verification(request_id="d2", actor_id="v1",
                                             task_id=conflict, decision="approved")

    def test_rejection_marks_material_rejected(self):
        result = self._material()
        authenticity, _ = result["verification_task_ids"]
        final = self.service.decide_verification(request_id="d1", actor_id="v1",
                                                 task_id=authenticity, decision="rejected",
                                                 note="证明与来源不符")
        self.assertEqual("rejected", final["material_status"])

    def test_decided_task_cannot_be_decided_again(self):
        result = self._material()
        authenticity, _ = result["verification_task_ids"]
        self.service.decide_verification(request_id="d1", actor_id="v1",
                                         task_id=authenticity, decision="approved")
        with self.assertRaises(ConflictError):
            self.service.decide_verification(request_id="d2", actor_id="v1",
                                             task_id=authenticity, decision="approved")


class ReviewAndConclusionTest(RightsChainTestBase):
    def test_unverified_material_blocks_enter_review(self):
        material = self._material()
        version = self.service.create_version(request_id="version", actor_id="c1", work_id="w1",
                                              version_no=1,
                                              material_ids=[material["material_id"]])
        with self.assertRaises(ValidationError):
            self.service.enter_review(request_id="enter", actor_id="op1",
                                      version_id=version["version_id"], stage="semi_final")

    def test_enter_review_generates_usable_conclusion(self):
        version_id, material_ids, _, conclusion = self._version_in_review()
        self.assertTrue(conclusion["usable"])
        self.assertEqual(1, conclusion["sequence"])
        detail = self.service.get_conclusion("a1", conclusion["conclusion_id"])
        self.assertEqual(3, len(detail["basis"]["materials"]))
        self.assertEqual("semi_final", detail["basis"]["stage"])
        self.assertIn("图片来源：市档案馆", detail["basis"]["required_attributions"])

    def test_revoke_generates_new_conclusion_and_impact(self):
        version_id, _, license_ids, first = self._version_in_review()
        collaboration = self.service.create_collaboration(
            request_id="collab", actor_id="o1f", version_id=version_id,
            partner="渠道商", channel="电商")
        revoked = self.service.revoke_license(request_id="revoke", actor_id="o1f",
                                              license_id=license_ids[0], reason="授权人撤回")
        impact = revoked["impact"]
        self.assertEqual(version_id, impact["affected_versions"][0]["version_id"])
        self.assertEqual("semi_final", impact["affected_versions"][0]["stage"])
        self.assertEqual(collaboration["collaboration_id"],
                         impact["unsigned_collaborations"][0]["collaboration_id"])
        conclusions = self.service.list_conclusions("a1", version_id)
        self.assertEqual(2, len(conclusions))
        self.assertEqual("superseded", conclusions[0]["standing"])
        self.assertEqual("current", conclusions[1]["standing"])
        self.assertFalse(conclusions[1]["usable"])

    def test_old_conclusion_basis_is_preserved(self):
        _, material_ids, license_ids, first = self._version_in_review()
        before = self.service.get_conclusion("a1", first["conclusion_id"])
        self.service.revoke_license(request_id="revoke", actor_id="o1f",
                                    license_id=license_ids[0], reason="授权人撤回")
        after = self.service.get_conclusion("a1", first["conclusion_id"])
        self.assertEqual(before["basis"], after["basis"])
        self.assertEqual(before["basis_hash"], after["basis_hash"])
        target = next(item for item in after["basis"]["materials"]
                      if item["material_id"] == material_ids[0])
        self.assertEqual("active", target["licenses"][0]["status"])

    def test_supplementary_license_after_review_generates_new_conclusion(self):
        version_id, material_ids, license_ids, _ = self._version_in_review()
        self.service.revoke_license(request_id="revoke", actor_id="o1f",
                                    license_id=license_ids[0], reason="授权人撤回")
        result = self.service.submit_license(
            request_id="license-2", actor_id="c1", material_id=material_ids[0],
            territory="全球", usage_scope="展览与衍生", valid_from="2026-10-01",
            valid_until="2028-12-31", required_attribution="署名")
        self.assertEqual(version_id, result["refreshed_versions"][0]["version_id"])
        conclusions = self.service.list_conclusions("a1", version_id)
        self.assertEqual(3, len(conclusions))
        self.assertTrue(conclusions[-1]["usable"])

    def test_supplementary_evidence_after_review_generates_new_conclusion(self):
        version_id, material_ids, _, _ = self._version_in_review()
        result = self.service.upload_evidence(request_id="ev-2", actor_id="c1",
                                              material_id=material_ids[0],
                                              content="补充授权函", summary="补充授权函")
        self.assertFalse(result["deduplicated"])
        self.assertEqual(version_id, result["refreshed_versions"][0]["version_id"])
        self.assertEqual(2, len(self.service.list_conclusions("a1", version_id)))

    def test_stage_must_advance_step_by_step(self):
        version_id, _, _, _ = self._version_in_review(stage="preliminary")
        with self.assertRaises(ConflictError):
            self.service.advance_stage(request_id="jump", actor_id="op1",
                                       version_id=version_id, stage="final")
        advanced = self.service.advance_stage(request_id="next", actor_id="op1",
                                              version_id=version_id, stage="semi_final")
        self.assertEqual("semi_final", advanced["stage"])

    def test_license_expiry_marks_and_reports(self):
        self._material(request_id="mat-exp", title="临期素材",
                       license={"territory": "中国大陆", "usage_scope": "展览",
                                "valid_from": "2026-01-01", "valid_until": "2026-10-02",
                                "required_attribution": "署名"})
        result = self.service.expire_licenses(request_id="expire-1", actor_id="o1f")
        self.assertEqual([], result["expired_license_ids"])
        self.service.clock = FixedClock(datetime(2026, 10, 3, tzinfo=timezone.utc))
        result = self.service.expire_licenses(request_id="expire-2", actor_id="o1f")
        self.assertEqual(1, len(result["expired_license_ids"]))
        impact = self.service.license_impact("o1f", result["expired_license_ids"][0])
        self.assertEqual("expired", impact["license_status"])


class EvidenceTest(RightsChainTestBase):
    def test_same_content_upload_is_idempotent(self):
        material = self._material(evidence=[])
        first = self.service.upload_evidence(request_id="ev-1", actor_id="c1",
                                             material_id=material["material_id"],
                                             content="授权书扫描件", summary="授权书")
        second = self.service.upload_evidence(request_id="ev-2", actor_id="c1",
                                              material_id=material["material_id"],
                                              content="授权书扫描件", summary="授权书")
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["evidence_id"], second["evidence_id"])
        detail = self.service.get_material("a1", material["material_id"])
        self.assertEqual(1, len(detail["evidence"]))

    def test_same_request_id_replays_upload(self):
        material = self._material(evidence=[])
        first = self.service.upload_evidence(request_id="ev-1", actor_id="c1",
                                             material_id=material["material_id"],
                                             content="授权书", summary="授权书")
        replay = self.service.upload_evidence(request_id="ev-1", actor_id="c1",
                                              material_id=material["material_id"],
                                              content="授权书", summary="授权书")
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["evidence_id"], replay["evidence_id"])

    def test_sensitive_evidence_is_redacted_for_judge(self):
        material = self._material()
        judge_view = self.service.get_material("j1", material["material_id"])
        self.assertEqual("[受限]", judge_view["evidence"][0]["summary"])
        self.assertIsNone(judge_view["evidence"][0]["content"])
        self.assertEqual(64, len(judge_view["evidence"][0]["content_hash"]))

    def test_sensitive_evidence_visible_to_licensing_officer(self):
        material = self._material()
        view = self.service.get_material("o1f", material["material_id"])
        self.assertEqual("授权邮件", view["evidence"][0]["summary"])

    def test_anonymous_reader_is_redacted(self):
        material = self._material()
        view = self.service.get_material("", material["material_id"])
        self.assertEqual("[受限]", view["evidence"][0]["summary"])


class TraceabilityTest(RightsChainTestBase):
    def test_licensing_officer_traces_full_basis(self):
        _, _, _, conclusion = self._version_in_review()
        trace = self.service.trace_conclusion_basis("o1f", conclusion["conclusion_id"])
        self.assertTrue(trace["basis_hash_valid"])
        self.assertEqual(3, len(trace["basis"]["materials"]))
        self.assertEqual(3, len(trace["evidence"]))
        self.assertTrue(any(item["summary"] == "授权邮件" for item in trace["evidence"]))
        actions = {item["action"] for item in trace["audit_trail"]}
        self.assertIn("rights.material.registered", actions)
        self.assertIn("rights.conclusion.generated", actions)

    def test_judge_cannot_trace_basis(self):
        _, _, _, conclusion = self._version_in_review()
        with self.assertRaises(PermissionDenied):
            self.service.trace_conclusion_basis("j1", conclusion["conclusion_id"])


class ObjectionTest(RightsChainTestBase):
    def _objection_on_material(self):
        version_id, material_ids, _, _ = self._version_in_review()
        objection = self.service.file_objection(request_id="obj", actor_id="c2",
                                                target_type="material",
                                                target_id=material_ids[0],
                                                reason="许可范围有异议")
        return version_id, material_ids, objection["objection_id"]

    def test_full_objection_flow_to_settlement(self):
        version_id, material_ids, objection_id = self._objection_on_material()
        preserved = self.service.preserve_evidence(request_id="preserve", actor_id="h1",
                                                   objection_id=objection_id)
        self.assertEqual("preserved", preserved["status"])
        self.assertEqual(1, preserved["evidence_count"])
        frozen = self.service.freeze_targets(
            request_id="freeze", actor_id="h1", objection_id=objection_id,
            targets=[{"target_type": "material", "target_id": material_ids[0],
                      "scope_note": "冻结该素材商业化使用"}])
        self.assertFalse(frozen["refreshed_versions"][0]["usable"])
        material = self.service.get_material("a1", material_ids[0])
        self.assertTrue(material["frozen"])
        settled = self.service.settle_objection(request_id="settle", actor_id="h1",
                                                objection_id=objection_id,
                                                resolution="双方和解：补充署名")
        self.assertEqual("settled", settled["status"])
        self.assertTrue(settled["refreshed_versions"][0]["usable"])
        material = self.service.get_material("a1", material_ids[0])
        self.assertFalse(material["frozen"])
        detail = self.service.get_objection("a1", objection_id)
        self.assertEqual(1, len(detail["preservations"]))
        self.assertEqual("lifted", detail["freezes"][0]["status"])

    def test_preservation_is_idempotent_per_evidence(self):
        _, _, objection_id = self._objection_on_material()
        first = self.service.preserve_evidence(request_id="preserve-1", actor_id="h1",
                                               objection_id=objection_id)
        second = self.service.preserve_evidence(request_id="preserve-2", actor_id="h1",
                                                objection_id=objection_id)
        self.assertEqual(1, first["new_preservations"])
        self.assertEqual(0, second["new_preservations"])

    def test_freeze_is_partial(self):
        version_id, material_ids, objection_id = self._objection_on_material()
        self.service.freeze_targets(
            request_id="freeze", actor_id="h1", objection_id=objection_id,
            targets=[{"target_type": "material", "target_id": material_ids[0],
                      "scope_note": "仅冻结争议素材"}])
        self.assertTrue(self.service.get_material("a1", material_ids[0])["frozen"])
        self.assertFalse(self.service.get_material("a1", material_ids[1])["frozen"])
        version = self.service.get_version("a1", version_id)
        self.assertFalse(version["frozen"])
        advanced = self.service.advance_stage(request_id="advance", actor_id="op1",
                                              version_id=version_id, stage="final")
        self.assertEqual("final", advanced["stage"])

    def test_double_freeze_conflicts(self):
        _, material_ids, objection_id = self._objection_on_material()
        target = [{"target_type": "material", "target_id": material_ids[0], "scope_note": "冻结"}]
        self.service.freeze_targets(request_id="freeze-1", actor_id="h1",
                                    objection_id=objection_id, targets=target)
        with self.assertRaises(ConflictError):
            self.service.freeze_targets(request_id="freeze-2", actor_id="h1",
                                        objection_id=objection_id, targets=target)

    def test_adjudication_sustained_on_conclusion(self):
        version_id, _, _, conclusion = self._version_in_review()
        objection = self.service.file_objection(request_id="obj", actor_id="c2",
                                                target_type="conclusion",
                                                target_id=conclusion["conclusion_id"],
                                                reason="结论依据不足")
        result = self.service.adjudicate_objection(
            request_id="adj", actor_id="h1", objection_id=objection["objection_id"],
            outcome="sustained", resolution="裁定成立：原结论无效")
        self.assertEqual(conclusion["conclusion_id"], result["invalidated_conclusion_id"])
        old = self.service.get_conclusion("a1", conclusion["conclusion_id"])
        self.assertEqual("invalidated", old["standing"])
        conclusions = self.service.list_conclusions("a1", version_id)
        self.assertEqual("current", conclusions[-1]["standing"])

    def test_adjudication_sustained_on_material_breaks_chain(self):
        version_id, material_ids, objection_id = self._objection_on_material()
        result = self.service.adjudicate_objection(
            request_id="adj", actor_id="h1", objection_id=objection_id,
            outcome="sustained", resolution="裁定成立：素材授权无效")
        self.assertFalse(result["refreshed_versions"][0]["usable"])
        material = self.service.get_material("a1", material_ids[0])
        self.assertEqual("rejected", material["status"])

    def test_creator_cannot_handle_objection(self):
        _, _, objection_id = self._objection_on_material()
        with self.assertRaises(PermissionDenied):
            self.service.preserve_evidence(request_id="preserve", actor_id="c1",
                                           objection_id=objection_id)

    def test_closed_objection_cannot_be_frozen(self):
        _, material_ids, objection_id = self._objection_on_material()
        self.service.settle_objection(request_id="settle", actor_id="h1",
                                      objection_id=objection_id, resolution="和解")
        with self.assertRaises(ConflictError):
            self.service.freeze_targets(
                request_id="freeze", actor_id="h1", objection_id=objection_id,
                targets=[{"target_type": "material", "target_id": material_ids[0],
                          "scope_note": "冻结"}])


class CollaborationTest(RightsChainTestBase):
    def setUp(self):
        super().setUp()
        self.service.set_team_representative(request_id="rep", actor_id="op1",
                                             work_id="w1", member_id="c1")

    def test_only_representative_can_sign(self):
        version_id, _, _, _ = self._version_in_review()
        collaboration = self.service.create_collaboration(
            request_id="collab", actor_id="o1f", version_id=version_id,
            partner="渠道商", channel="电商")
        with self.assertRaises(PermissionDenied):
            self.service.sign_collaboration(request_id="sign", actor_id="c2",
                                            collaboration_id=collaboration["collaboration_id"])
        signed = self.service.sign_collaboration(request_id="sign-2", actor_id="c1",
                                                 collaboration_id=collaboration["collaboration_id"])
        self.assertEqual("signed", signed["status"])

    def test_sign_requires_usable_conclusion(self):
        version_id, _, license_ids, _ = self._version_in_review()
        collaboration = self.service.create_collaboration(
            request_id="collab", actor_id="o1f", version_id=version_id,
            partner="渠道商", channel="电商")
        self.service.revoke_license(request_id="revoke", actor_id="o1f",
                                    license_id=license_ids[0], reason="撤回")
        with self.assertRaises(ConflictError):
            self.service.sign_collaboration(request_id="sign", actor_id="c1",
                                            collaboration_id=collaboration["collaboration_id"])

    def test_impact_lists_only_unsigned_collaborations(self):
        version_id, _, license_ids, _ = self._version_in_review()
        signed = self.service.create_collaboration(request_id="collab-1", actor_id="o1f",
                                                   version_id=version_id, partner="渠道商甲",
                                                   channel="电商")
        self.service.create_collaboration(request_id="collab-2", actor_id="o1f",
                                          version_id=version_id, partner="渠道商乙",
                                          channel="线下")
        self.service.sign_collaboration(request_id="sign", actor_id="c1",
                                        collaboration_id=signed["collaboration_id"])
        revoked = self.service.revoke_license(request_id="revoke", actor_id="o1f",
                                              license_id=license_ids[0], reason="撤回")
        unsigned = revoked["impact"]["unsigned_collaborations"]
        self.assertEqual(1, len(unsigned))
        self.assertEqual("渠道商乙", unsigned[0]["partner"])

    def test_missing_representative_blocks_signing(self):
        self.service.register_work(request_id="work-2", actor_id="c1", site_id="s1",
                                   work_id="w2", title="无代表作品",
                                   team=[{"member_id": "c1", "share_percent": 100}])
        material = self._verified_material(request_id="mat-w2", work_id="w2")
        version = self.service.create_version(request_id="version-w2", actor_id="c1",
                                              work_id="w2", version_no=1,
                                              material_ids=[material["material_id"]])
        self.service.enter_review(request_id="enter-w2", actor_id="op1",
                                  version_id=version["version_id"], stage="preliminary")
        collaboration = self.service.create_collaboration(
            request_id="collab-w2", actor_id="o1f", version_id=version["version_id"],
            partner="渠道商", channel="电商")
        with self.assertRaises(ConflictError):
            self.service.sign_collaboration(request_id="sign-w2", actor_id="c1",
                                            collaboration_id=collaboration["collaboration_id"])


class RecoveryTest(unittest.TestCase):
    def test_queue_and_hashes_survive_restart(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rights.sqlite3"
            database = RightsDatabase(path)
            foundation = DomainService(database, CLOCK)
            service = RightsChainService(database, CLOCK)
            foundation.register_organization(request_id="org", actor_id="bootstrap",
                                             organization_id="o1", name="组委会")
            foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                      display_name="管理员", role="admin", organization_id="o1")
            foundation.register_actor(request_id="creator", actor_id="a1", new_actor_id="c1",
                                      display_name="创作者", role="creator", organization_id="o1")
            foundation.register_site(request_id="site", actor_id="a1", site_id="s1",
                                     organization_id="o1", name="节点",
                                     timezone_name="Asia/Shanghai")
            service.register_work(request_id="work", actor_id="c1", site_id="s1", work_id="w1",
                                  title="茶礼",
                                  team=[{"member_id": "c1", "share_percent": 100}])
            service.register_material(
                request_id="mat", actor_id="c1", site_id="s1", work_id="w1", title="老照片",
                source_type="老照片", source_description="馆藏照片",
                rights_holders=[{"name": "档案馆", "share_percent": 100}],
                license={"territory": "中国大陆", "usage_scope": "展览",
                         "valid_from": "2026-01-01", "valid_until": None,
                         "required_attribution": "署名"},
                evidence=[{"content": "授权邮件", "summary": "授权邮件", "sensitive": True}])
            queue_before = service.list_verification_tasks("a1")
            database.close()

            recovered = RightsDatabase(path)
            recovered_service = RightsChainService(recovered, CLOCK)
            queue_after = recovered_service.list_verification_tasks("a1")
            self.assertEqual([task["task_id"] for task in queue_before],
                             [task["task_id"] for task in queue_after])
            recovery = recovered_service.verify_recovery("a1")
            self.assertTrue(recovery["consistent"])
            self.assertEqual(2, recovery["pending_tasks"])
            self.assertEqual(1, recovery["evidence_total"])
            self.assertEqual([], recovery["evidence_hash_mismatches"])
            self.assertTrue(recovery["audit_valid"])
            recovered.close()


if __name__ == "__main__":
    unittest.main()
