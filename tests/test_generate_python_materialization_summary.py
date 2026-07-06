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


if __name__ == "__main__":
    unittest.main()
