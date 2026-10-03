"""文化素材权利链核验域的 HTTP 路由测试。"""

import unittest

from creative_program_foundation.rights_api import route
from creative_program_foundation.rights_schema import RIGHTS_SCHEMA
from creative_program_foundation.rights_service import RightsService
from creative_program_foundation.storage import Database


class RightsApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.database.connection.executescript(RIGHTS_SCHEMA)
        self.service = RightsService(self.database)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="lic-1"):
        return route(self.service, method, path, body or {}, {"X-Actor-Id": actor})

    def _bootstrap(self):
        self._call("POST", "/organizations",
                   {"request_id": "org-req", "organization_id": "o1", "name": "大赛"}, "bootstrap")
        self._call("POST", "/actors",
                   {"request_id": "admin-req", "new_actor_id": "admin-1", "display_name": "管理员",
                    "role": "admin", "organization_id": "o1"}, "bootstrap")
        for actor_id, name, role in [
            ("lic-1", "授权专员", "licensing_officer"),
            ("rev-1", "复核员", "conflict_reviewer"),
            ("judge-1", "评委", "judge"),
            ("creator-1", "创作者", "creator"),
        ]:
            self._call("POST", "/actors",
                       {"request_id": f"actor-req-{actor_id}", "new_actor_id": actor_id,
                        "display_name": name, "role": role, "organization_id": "o1"}, "admin-1")
        self._call("POST", "/sites",
                   {"request_id": "site-req", "site_id": "s1", "organization_id": "o1",
                    "name": "茶礼赛道", "timezone_name": "Asia/Shanghai"}, "admin-1")

    def _ready_material(self):
        self._call("POST", "/rworks",
                   {"request_id": "work-req", "site_id": "s1", "work_id": "w1", "title": "茶礼",
                    "primary_creator_id": "creator-1"}, "creator-1")
        self._call("POST", "/rights-holders",
                   {"request_id": "holder-req", "holder_id": "h1", "display_name": "档案馆",
                    "kind": "holder", "contact_summary": "窗口",
                    "can_accept_channel_deals": True}, "creator-1")
        self._call("POST", "/rmaterials",
                   {"request_id": "material-req", "work_id": "w1", "material_id": "m1", "kind": "photo",
                    "title": "老照片", "source_description": "馆藏",
                    "rights_holder_ids": ["h1"], "holder_shares": {"h1": 100},
                    "coauthor_ids": [], "territory": "*", "purposes": ["exhibition"],
                    "valid_from": "2026-01-01T00:00:00+08:00", "valid_until": None,
                    "required_attribution": "市档案馆"}, "creator-1")

    def test_health_still_served_by_base_route(self):
        status, payload = route(self.service, "GET", "/health", None, {})
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_full_flow_and_http_idempotent_replay(self):
        self._ready_material()
        status1, ev1 = self._call("POST", "/revidence", {
            "request_id": "evidence-req", "material_id": "m1", "evidence_id": "e1",
            "filename": "license.pdf", "media_type": "application/pdf",
            "summary": "授权书", "classification": "sensitive",
            "content_base64": "YWJj"})
        self.assertEqual(201, status1)
        # 同一 request_id 重放返回 200
        status_replay, ev_replay = self._call("POST", "/revidence", {
            "request_id": "evidence-req", "material_id": "m1", "evidence_id": "e1",
            "filename": "license.pdf", "media_type": "application/pdf",
            "summary": "授权书", "classification": "sensitive",
            "content_base64": "YWJj"})
        self.assertEqual(200, status_replay)
        self.assertEqual(ev1["resource_id"], ev_replay["resource_id"])
        # 相同内容换 request_id 仍幂等
        status_dup, ev_dup = self._call("POST", "/revidence", {
            "request_id": "evidence-dup-req", "material_id": "m1", "evidence_id": "e1-dup",
            "filename": "license2.pdf", "media_type": "application/pdf",
            "summary": "又传一次", "content_base64": "YWJj"})
        self.assertEqual(200, status_dup)
        self.assertEqual("e1", ev_dup["resource_id"])

        self._call("POST", "/verification-tasks",
                   {"request_id": "task-req", "material_id": "m1", "task_id": "task-1"})
        status, submitted = self._call(
            "POST", "/verification-tasks/task-1/submit",
            {"request_id": "submit-req", "decision": "authentic", "notes": "一致"})
        self.assertEqual(201, status)
        self.assertTrue(submitted["conclusion_id"])

    def test_trace_denied_for_judge_but_allowed_for_officer(self):
        self._ready_material()
        self._call("POST", "/verification-tasks",
                   {"request_id": "task-req", "material_id": "m1", "task_id": "task-1"})
        self._call("POST", "/verification-tasks/task-1/submit",
                   {"request_id": "submit-req", "decision": "authentic"})
        status, payload = self._call("GET", "/rtrace?material_id=m1", actor="judge-1")
        self.assertEqual(403, status)
        status, payload = self._call("GET", "/rtrace?material_id=m1", actor="lic-1")
        self.assertEqual(200, status)
        self.assertEqual("cleared", payload["current_conclusion"]["decision"])
        self.assertIn("license", payload["chain"][0]["basis"])

    def test_review_pack_redacts_sensitive_evidence_over_http(self):
        self._ready_material()
        self._call("POST", "/revidence", {
            "request_id": "evidence-req", "material_id": "m1", "evidence_id": "e1",
            "filename": "secret.pdf", "media_type": "application/pdf",
            "summary": "敏感底价", "classification": "sensitive",
            "content_base64": "c2VjcmV0"})
        status, judge_pack = self._call("GET", "/review-pack?work_id=w1", actor="judge-1")
        self.assertEqual(200, status)
        self.assertIn("已隐藏", judge_pack["materials"][0]["evidence"][0]["summary"])
        status, officer_pack = self._call("GET", "/review-pack?work_id=w1", actor="lic-1")
        self.assertEqual("敏感底价", officer_pack["materials"][0]["evidence"][0]["summary"])

    def test_dispute_freeze_blocks_review_entry_over_http(self):
        self._ready_material()
        self._call("POST", "/verification-tasks",
                   {"request_id": "task-req", "material_id": "m1", "task_id": "task-1"})
        self._call("POST", "/verification-tasks/task-1/submit",
                   {"request_id": "submit-req", "decision": "authentic"})
        self._call("POST", "/rwork-versions",
                   {"request_id": "version-req", "work_id": "w1", "version_id": "v1",
                    "material_ids": ["m1"]}, "creator-1")
        self._call("POST", "/rdisputes",
                   {"request_id": "dispute-req", "dispute_id": "d1", "work_id": "w1",
                    "material_id": "m1", "reason": "权属异议"}, "rev-1")
        status, payload = self._call(
            "POST", "/rdisputes/d1/freeze",
            {"request_id": "freeze-req", "version_id": "v1", "material_id": "m1"}, "rev-1")
        self.assertEqual(201, status)
        status, payload = self._call(
            "POST", "/rreviews/enter",
            {"request_id": "enter-req", "work_id": "w1", "stage": "preliminary",
             "version_id": "v1"})
        self.assertEqual(409, status)
        # 和解解除冻结后可进入评审
        self._call("POST", "/rdisputes/d1/resolve",
                   {"request_id": "resolve-req", "outcome": "settled", "terms": "补署名"}, "rev-1")
        status, payload = self._call(
            "POST", "/rreviews/enter",
            {"request_id": "enter-req-2", "work_id": "w1", "stage": "preliminary",
             "version_id": "v1"})
        self.assertEqual(201, status)

    def test_missing_actor_is_rejected(self):
        status, payload = route(self.service, "POST", "/rworks",
                                {"request_id": "work-req", "site_id": "s1", "work_id": "w1",
                                 "title": "x", "primary_creator_id": "creator-1"}, {})
        self.assertEqual(404, status)

    def test_unknown_rights_route_is_404(self):
        status, payload = route(self.service, "GET", "/no-such-rights-route", None, {})
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
