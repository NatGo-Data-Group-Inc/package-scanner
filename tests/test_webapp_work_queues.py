import unittest

from package_scanner.webapp_status import dashboard_attention_kind


class WebappWorkQueueTests(unittest.TestCase):
    def test_unapproved_successful_run_needs_package_approval(self):
        self.assertEqual(dashboard_attention_kind({"status": "SUCCEEDED", "approved": False}), "approval")

    def test_live_package_validation_approval_phase_needs_package_approval(self):
        self.assertEqual(dashboard_attention_kind({"status": "RUNNING", "current_phase": "awaiting-approval"}), "approval")

    def test_approved_run_waiting_for_materialization(self):
        self.assertEqual(
            dashboard_attention_kind({"status": "SUCCEEDED", "approved": True, "awaiting_materialization": True}),
            "materialization",
        )

    def test_materialize_phase_is_visible_as_build_work(self):
        self.assertEqual(dashboard_attention_kind({"status": "RUNNING", "current_phase": "materialize"}), "materialization")


if __name__ == "__main__":
    unittest.main()
