import importlib
import unittest
from unittest import mock

try:
    from webapp.app import stage_steps_for_run
except ModuleNotFoundError as exc:
    if exc.name != "flask":
        raise
    WEBAPP_DEPENDENCY_MISSING = True
else:
    WEBAPP_DEPENDENCY_MISSING = False


@unittest.skipIf(WEBAPP_DEPENDENCY_MISSING, "Flask is not installed in this test environment")
class WebappPreflightTests(unittest.TestCase):
    # Stage names are the ones run-python-ecs-task.sh writes with write_state.
    def test_python_stage_progress_marks_earlier_gates_done(self):
        steps = stage_steps_for_run("python", "materialize", "RUNNING")
        self.assertEqual(
            [(step["name"], step["status"]) for step in steps[:4]],
            [
                ("Package Validation", "done"),
                ("Awaiting Approval", "done"),
                ("Materialize", "current"),
                ("Restored", "pending"),
            ],
        )

    def test_package_validation_failure_marks_that_step_failed(self):
        steps = stage_steps_for_run("python", "package-validation", "FAILED")
        self.assertEqual(steps[0]["status"], "failed")
        self.assertEqual(steps[1]["status"], "pending")

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
