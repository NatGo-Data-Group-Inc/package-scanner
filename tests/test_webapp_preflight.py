import importlib
import unittest
from unittest import mock

try:
    from webapp.app import preflight_summary_view, stage_steps_for_run
except ModuleNotFoundError as exc:
    if exc.name != "flask":
        raise
    WEBAPP_DEPENDENCY_MISSING = True
else:
    WEBAPP_DEPENDENCY_MISSING = False


@unittest.skipIf(WEBAPP_DEPENDENCY_MISSING, "Flask is not installed in this test environment")
class WebappPreflightTests(unittest.TestCase):
    def test_python_stage_progress_includes_both_preflight_gates(self):
        steps = stage_steps_for_run("python", "preflight-installed", "RUNNING")
        self.assertEqual(
            [(step["name"], step["status"]) for step in steps[:3]],
            [
                ("Preflight Plan", "done"),
                ("Materialize", "done"),
                ("Preflight Installed", "current"),
            ],
        )

    def test_plan_preflight_failure_marks_the_plan_step_failed(self):
        steps = stage_steps_for_run("python", "preflight-plan-failed", "FAILED")
        self.assertEqual(steps[0]["status"], "failed")
        self.assertEqual(steps[1]["status"], "pending")

    def test_preflight_summary_view_exposes_gate_and_inventory_status(self):
        view = preflight_summary_view(
            {
                "package_count": 12,
                "vulnerability_gate": {
                    "status": "pass",
                    "blocking_findings": 0,
                    "findings_by_severity": {"MEDIUM": 2},
                },
                "installed_environment": {"inventory_matches_dry_plan": True},
            }
        )
        self.assertEqual(view["gate_status"], "PASS")
        self.assertEqual(view["findings_display"], "Medium: 2")
        self.assertTrue(view["inventory_matches"])

    def test_start_preflight_route_returns_started_run_page(self):
        webapp_module = importlib.import_module("webapp.app")

        def fake_aws_json(args, **_kwargs):
            if args[0] == "cloudformation":
                return {"Stacks": [{"Outputs": [
                    {"OutputKey": "InputBucketName", "OutputValue": "input-bucket"},
                    {"OutputKey": "EvidenceBucketName", "OutputValue": "evidence-bucket"},
                    {"OutputKey": "EphemeralBucketName", "OutputValue": "ephemeral-bucket"},
                    {"OutputKey": "PythonScanOrchestrationStateMachineArn", "OutputValue": "state-machine-arn"},
                ]}]}
            if args[:2] == ["stepfunctions", "start-execution"]:
                return {"executionArn": "execution-arn"}
            self.fail(f"Unexpected AWS CLI request: {args}")

        with (
            mock.patch.object(webapp_module, "aws_json", side_effect=fake_aws_json),
            mock.patch.object(webapp_module, "stepfunctions_list_executions", return_value=[]),
            mock.patch.object(webapp_module, "s3_put_bytes"),
        ):
            response = webapp_module.app.test_client().post(
                "/candidates/python/bottom-solve/start",
                data={"platform": "linux-amd64"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"bottom-solve", response.data)
        self.assertIn(b"execution-arn", response.data)


if __name__ == "__main__":
    unittest.main()
