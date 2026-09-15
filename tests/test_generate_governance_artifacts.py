import importlib.util
import json
import shutil
import unittest
import uuid
from pathlib import Path


def load_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "generate-governance-artifacts.py"
    spec = importlib.util.spec_from_file_location("governance", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class GovernanceGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()
        cls.tmp_root = Path(__file__).resolve().parents[1] / ".tmp-tests"
        cls.tmp_root.mkdir(parents=True, exist_ok=True)

    def test_main_generates_manifest_and_summary(self):
        run_dir = self.tmp_root / f"run-{uuid.uuid4().hex}"
        run_dir.mkdir(parents=True, exist_ok=False)
        try:
            (run_dir / "requirements.lock.txt").write_text(
                "requests==2.31.0\nurllib3==2.2.0\n", encoding="utf-8"
            )
            (run_dir / "trivy-sbom-report.json").write_text(
                json.dumps(
                    {
                        "Results": [
                            {
                                "Vulnerabilities": [
                                    {
                                        "PkgName": "requests",
                                        "InstalledVersion": "2.31.0",
                                        "VulnerabilityID": "CVE-2024-0001",
                                        "Severity": "HIGH",
                                        "FixedVersion": "2.32.0",
                                        "Title": "example vuln",
                                        "PrimaryURL": "https://example.test/cve",
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "safety-report.json").write_text(
                json.dumps(
                    {
                        "vulnerabilities": [
                            {
                                "package_name": "urllib3",
                                "analyzed_version": "2.2.0",
                                "vulnerability_id": "CVE-2024-0002",
                                "severity": "MEDIUM",
                                "fixed_versions": ["2.2.1"],
                                "advisory": "example safety vuln",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            args = [
                "--run-dir",
                str(run_dir),
                "--platform",
                "linux-amd64",
                "--remediate-medium",
                "true",
                "--fail-on-medium",
                "false",
            ]
            with self.assertRaises(SystemExit) as exc:
                self.mod.main(args)
            self.assertEqual(exc.exception.code, 3)

            summary = json.loads((run_dir / "governance-summary.json").read_text())
            self.assertEqual(summary["counts"]["approval_candidates"], 2)
            self.assertEqual(summary["counts"]["findings_total"], 2)
            self.assertEqual(summary["counts"]["findings_by_scanner"]["trivy"], 1)
            self.assertEqual(summary["counts"]["findings_by_scanner"]["safety"], 1)
            manifest = json.loads(
                (run_dir / "governance-artifact-manifest.json").read_text()
            )
            self.assertGreaterEqual(len(manifest), 5)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_parse_requires_strict_schema(self):
        run_dir = self.tmp_root / f"run-{uuid.uuid4().hex}"
        run_dir.mkdir(parents=True, exist_ok=False)
        try:
            bad = run_dir / "safety-report.json"
            bad.write_text('{"unexpected": true}', encoding="utf-8")
            with self.assertRaises(self.mod.GovernanceError):
                self.mod.parse_safety(bad)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_preflight_resolved_package_envelope_produces_approval_manifest(self):
        run_dir = self.tmp_root / f"run-{uuid.uuid4().hex}"
        run_dir.mkdir(parents=True, exist_ok=False)
        try:
            (run_dir / "requirements.lock.txt").write_text("pip==24.0\n", encoding="utf-8")
            (run_dir / "conda-list.json").write_text(
                json.dumps({
                    "platform": "linux-64",
                    "packages": [
                        {"name": "python", "version": "3.12.1", "build": "h123_0"},
                        {"name": "numpy", "version": "2.1.0", "build": "py312_0"},
                    ],
                }),
                encoding="utf-8",
            )
            (run_dir / "trivy-sbom-report.json").write_text(
                '{"Results": []}', encoding="utf-8"
            )
            (run_dir / "safety-report.json").write_text("[]", encoding="utf-8")

            result = self.mod.main([
                "--run-dir", str(run_dir),
                "--platform", "linux-amd64",
            ])
            self.assertTrue((run_dir / "approval-candidate-packages.csv").exists())
            self.assertEqual(self.mod.parse_conda_list(run_dir / "conda-list.json")[0]["package_name"], "python")
            summary = result["summary"]
            self.assertEqual(summary["counts"]["approval_candidates"], 3)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
