import unittest

from rights_chain.acceptance import run


class RightsAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["current_usable"])
        self.assertTrue(result["old_basis_kept"])
        self.assertTrue(result["impact_hit"])
        self.assertTrue(result["judge_redacted"])
        self.assertTrue(result["trace_complete"])
        self.assertTrue(result["evidence_deduplicated"])
        self.assertTrue(result["queue_consistent"])
        self.assertTrue(result["recovery_consistent"])
        self.assertEqual(2, result["pending_after_recovery"])


if __name__ == "__main__":
    unittest.main()
