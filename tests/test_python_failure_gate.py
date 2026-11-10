import re
import subprocess
import unittest
from pathlib import Path


def bash_major_version():
    result = subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True)
    return int(result.stdout.strip() or 0)


class PythonFailureGateTests(unittest.TestCase):
    # bash 3.2 (the macOS default) does not run an ERR trap when the failure
    # comes from a subshell such as `(exit 1)`. The scan container runs bash
    # 4.4 on Rocky Linux 8, which does. On macOS: brew install bash.
    @unittest.skipIf(
        bash_major_version() < 4,
        "needs bash 4+ to match the scan container; macOS ships bash 3.2 (brew install bash)",
    )
    def test_failure_exits_even_when_diagnostics_fail(self):
        source = (Path(__file__).resolve().parents[1] / "scripts/run-python-ecs-task.sh").read_text()
        handler = re.search(r"publish_failure_diagnostics\(\) \{.*?\n\}", source, re.S).group()
        for status in (1, 2, 3, 137):
            for diagnostics_status in (0, 42):
                with self.subTest(status=status, diagnostics_status=diagnostics_status):
                    script = "\n".join([
                        source.splitlines()[1],
                        'RUN_DIR=/tmp; CHECKPOINT_PREFIX=unused',
                        'stop_checkpoint_loop() { :; }',
                        'write_state() { echo "state:$1"; }',
                        f'upload_if_exists() {{ return {diagnostics_status}; }}',
                        f'publish_package_validation_artifacts() {{ echo "evidence:$1"; return {diagnostics_status}; }}',
                        f'publish_checkpoint() {{ return {diagnostics_status}; }}',
                        handler,
                        'trap publish_failure_diagnostics ERR',
                        f'preflight() {{ (exit {status}); echo incorrectly-resumed-preflight; }}',
                        'preflight',
                        'echo incorrectly-started-materialization',
                    ])
                    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertIn('state:failed', result.stdout)
                    self.assertIn('evidence:plan', result.stdout)
                    self.assertNotIn('incorrectly-', result.stdout)
