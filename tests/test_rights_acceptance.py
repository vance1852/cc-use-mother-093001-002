import unittest

from creative_program_foundation.rights_acceptance import run


class RightsAcceptanceTest(unittest.TestCase):
    def test_rights_chain_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["evidence_idempotent"])
        self.assertTrue(result["pinned_basis_unchanged"])
        self.assertEqual(["cleared", "restricted"], result["photo_conclusion_chain"])
        self.assertEqual("m-rubbing", result["expired_material"])
        self.assertIn("v1", result["expired_impact_versions"])
        self.assertIn("deal-book", result["expired_unsigned_deals"])
        self.assertTrue(result["deal_signing_blocked_during_freeze"])
        self.assertTrue(result["judge_sensitive_redacted"])
        self.assertTrue(result["queue_consistent_after_restart"])
        self.assertTrue(result["evidence_hash_consistent_after_restart"])
        self.assertTrue(result["audit_valid_after_restart"])


if __name__ == "__main__":
    unittest.main()
