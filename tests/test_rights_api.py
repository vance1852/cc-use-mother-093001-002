import unittest

from creative_program_foundation.service import DomainService
from rights_chain.api import route
from rights_chain.service import RightsChainService
from rights_chain.storage import RightsDatabase


class RightsApiTest(unittest.TestCase):
    def setUp(self):
        self.database = RightsDatabase()
        self.foundation = DomainService(self.database)
        self.service = RightsChainService(self.database)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="组委会")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                       display_name="管理员", role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="creator", actor_id="a1", new_actor_id="c1",
                                       display_name="创作者", role="creator", organization_id="o1")
        self.foundation.register_actor(request_id="judge", actor_id="a1", new_actor_id="j1",
                                       display_name="评委", role="judge", organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="a1", site_id="s1",
                                      organization_id="o1", name="节点",
                                      timezone_name="Asia/Shanghai")
        self.service.register_work(request_id="work", actor_id="c1", site_id="s1", work_id="w1",
                                   title="茶礼",
                                   team=[{"member_id": "c1", "share_percent": 100}])

    def tearDown(self):
        self.database.close()

    def _material_body(self):
        return {"request_id": "mat", "site_id": "s1", "work_id": "w1", "title": "老照片",
                "source_type": "老照片", "source_description": "馆藏照片",
                "rights_holders": [{"name": "档案馆", "share_percent": 100}],
                "license": {"territory": "中国大陆", "usage_scope": "展览",
                            "valid_from": "2026-01-01", "valid_until": "2027-12-31",
                            "required_attribution": "署名"},
                "evidence": [{"content": "授权邮件", "summary": "授权邮件", "sensitive": True}]}

    def test_foundation_routes_still_work(self):
        status, payload = route(self.service, self.foundation, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_rights_route_returns_404(self):
        status, payload = route(self.service, self.foundation, "GET", "/rights/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_register_material_and_replay(self):
        body = self._material_body()
        status, first = route(self.service, self.foundation, "POST", "/rights/materials", body,
                              {"X-Actor-Id": "c1"})
        self.assertEqual(201, status)
        self.assertFalse(first["replayed"])
        status, second = route(self.service, self.foundation, "POST", "/rights/materials", body,
                               {"X-Actor-Id": "c1"})
        self.assertEqual(200, status)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["material_id"], second["material_id"])

    def test_missing_field_returns_400(self):
        status, payload = route(self.service, self.foundation, "POST", "/rights/materials",
                                {"request_id": "x"}, {"X-Actor-Id": "c1"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_judge_gets_redacted_material_via_api(self):
        _, created = route(self.service, self.foundation, "POST", "/rights/materials",
                           self._material_body(), {"X-Actor-Id": "c1"})
        path = f"/rights/materials/{created['material_id']}"
        _, judge_view = route(self.service, self.foundation, "GET", path, None,
                              {"X-Actor-Id": "j1"})
        self.assertEqual("[受限]", judge_view["evidence"][0]["summary"])
        _, admin_view = route(self.service, self.foundation, "GET", path, None,
                              {"X-Actor-Id": "a1"})
        self.assertEqual("授权邮件", admin_view["evidence"][0]["summary"])

    def test_verification_queue_via_api(self):
        route(self.service, self.foundation, "POST", "/rights/materials",
              self._material_body(), {"X-Actor-Id": "c1"})
        status, payload = route(self.service, self.foundation, "GET",
                                "/rights/verification-tasks?status=pending", None,
                                {"X-Actor-Id": "a1"})
        self.assertEqual(200, status)
        self.assertEqual(2, len(payload["items"]))

    def test_recovery_check_requires_privileged_role(self):
        status, _ = route(self.service, self.foundation, "GET", "/rights/recovery-check", None,
                          {"X-Actor-Id": "j1"})
        self.assertEqual(403, status)
        status, payload = route(self.service, self.foundation, "GET", "/rights/recovery-check",
                                None, {"X-Actor-Id": "a1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["consistent"])

    def test_error_response_uses_domain_error_shape(self):
        status, payload = route(self.service, self.foundation, "POST", "/rights/materials",
                                self._material_body(), {"X-Actor-Id": "j1"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])


if __name__ == "__main__":
    unittest.main()
