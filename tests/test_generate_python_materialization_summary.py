import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "generate-python-materialization-summary.py"
    spec = importlib.util.spec_from_file_location("python_materialization_summary", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class PythonMaterializationSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="python-summary-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_main(self):
        argv = [
            "generate-python-materialization-summary.py",
            "--run-dir",
            str(self.tmpdir),
            "--platform",
            "linux-amd64",
            "--python-version",
            "3.11.9",
            "--root-prefix",
            "/tmp/root",
            "--env-prefix",
            "/tmp/env",
        ]
        with mock.patch.object(sys, "argv", argv):
            return self.mod.main()

    def test_combined_realized_inventory_and_aliases_satisfy_requests(self):
        (self.tmpdir / "environment.yml").write_text(
            "\n".join(
                [
                    "name: test-env",
                    "channels:",
                    "  - conda-forge",
                    "dependencies:",
                    "  - python",
                    "  - libgcc-ng",
                    "  - pytorch",
                    "  - pyyaml",
                    "  - requests",
                    "  - pip",
                    "  - pip:",
                    "      - asksageclient",
                    "      - fhir.resources",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (self.tmpdir / "conda-list.json").write_text(
            json.dumps(
                [
                    {"name": "python", "version": "3.11.9"},
                    {"name": "pip", "version": "24.0"},
                    {"name": "libgcc-ng", "version": "14.2.0"},
                ]
            ),
            encoding="utf-8",
        )
        (self.tmpdir / "requirements.lock.txt").write_text(
            "\n".join(
                [
                    "torch==2.7.0",
                    "PyYAML==6.0.2",
                    "requests==2.32.3",
                    "asksageclient==1.0.0",
                    "fhir-resources==8.1.0",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        exit_code = self.run_main()

        self.assertEqual(exit_code, 0)
        summary = json.loads((self.tmpdir / "materialization-summary.json").read_text())
        self.assertEqual(summary["missing_requested_conda_packages"], [])
        self.assertEqual(summary["missing_requested_pip_packages"], [])

    def test_still_reports_true_missing_packages(self):
        (self.tmpdir / "environment.yml").write_text(
            "\n".join(
                [
                    "name: test-env",
                    "channels:",
                    "  - conda-forge",
                    "dependencies:",
                    "  - python",
                    "  - definitely-missing-package",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (self.tmpdir / "conda-list.json").write_text(
            json.dumps([{"name": "python", "version": "3.11.9"}]),
            encoding="utf-8",
        )
        (self.tmpdir / "requirements.lock.txt").write_text("", encoding="utf-8")

        exit_code = self.run_main()

        self.assertEqual(exit_code, 0)
        summary = json.loads((self.tmpdir / "materialization-summary.json").read_text())
        self.assertEqual(
            summary["missing_requested_conda_packages"],
            ["definitely-missing-package"],
        )

    # --- conda-list.json shapes (defect: conda_package_count 0 failed good runs) ---

    def write_environment(self, *dependencies):
        lines = ["name: target", "channels:", "  - conda-forge", "dependencies:"]
        lines += [f"  - {dep}" for dep in dependencies]
        (self.tmpdir / "environment.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def summary(self):
        return json.loads((self.tmpdir / "materialization-summary.json").read_text())

    def test_micromamba_2_object_shape_is_read(self):
        # Shape written by `micromamba list --json` in the current image. Before the
        # fix this produced conda_package_count 0 and reported python as missing.
        self.write_environment("python=3.11.13", "absl-py=2.3.1")
        (self.tmpdir / "conda-list.json").write_text(
            json.dumps(
                {
                    "log_history": [],
                    "packages": [
                        {"name": "python", "version": "3.11.13", "channel": "conda-forge"},
                        {"name": "absl-py", "version": "2.3.1", "channel": "conda-forge"},
                        {"name": "pip", "version": "25.2", "channel": "conda-forge"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.tmpdir / "requirements.lock.txt").write_text("", encoding="utf-8")

        self.assertEqual(self.run_main(), 0)
        summary = self.summary()
        self.assertEqual(summary["conda_package_count"], 3)
        self.assertEqual(summary["missing_requested_conda_packages"], [])

    def test_legacy_list_shape_is_still_read(self):
        self.write_environment("python")
        (self.tmpdir / "conda-list.json").write_text(
            json.dumps([{"name": "python", "version": "3.11.9"}]), encoding="utf-8"
        )
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.summary()["conda_package_count"], 1)
        self.assertEqual(self.summary()["missing_requested_conda_packages"], [])

    def test_missing_conda_list_means_no_conda_inventory(self):
        self.assertEqual(self.mod.read_conda_list(self.tmpdir / "conda-list.json"), [])

    def test_invalid_conda_list_is_an_error_not_an_empty_inventory(self):
        path = self.tmpdir / "conda-list.json"
        for content in ("", "not json", json.dumps({"unexpected": 1}), json.dumps("text")):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(self.mod.SummaryInputError):
                    self.mod.read_conda_list(path)

    # --- environment.yml (defect 7: an unreadable file passed validation) ---

    def test_unreadable_environment_is_an_error_not_an_empty_request(self):
        path = self.tmpdir / "environment.yml"
        cases = {
            "malformed": "dependencies: [unclosed\n",
            "no dependencies": "name: target\nchannels: [conda-forge]\n",
            "not a mapping": "- just\n- a list\n",
        }
        for label, content in cases.items():
            with self.subTest(label):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(self.mod.SummaryInputError):
                    self.mod.load_requested_packages(path)

    def test_missing_environment_is_an_error(self):
        with self.assertRaises(self.mod.SummaryInputError):
            self.mod.load_requested_packages(self.tmpdir / "environment.yml")

    def test_missing_pyyaml_is_an_error(self):
        self.write_environment("python")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            with self.assertRaises(self.mod.SummaryInputError) as raised:
                self.mod.load_requested_packages(self.tmpdir / "environment.yml")
        self.assertIn("PyYAML", str(raised.exception))

    def test_command_line_exits_5_with_a_clear_message(self):
        import subprocess

        (self.tmpdir / "environment.yml").write_text("dependencies: [unclosed\n", encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "generate-python-materialization-summary.py"
        result = subprocess.run(
            [sys.executable, str(script), "--run-dir", str(self.tmpdir), "--platform", "linux-amd64",
             "--python-version", "3.11.9", "--root-prefix", "/tmp/root", "--env-prefix", "/tmp/env"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("materialization-summary-error: environment.yml cannot be parsed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
