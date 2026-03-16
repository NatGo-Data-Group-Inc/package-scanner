import csv
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_script(script_name, module_name):
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class RArtifactGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sbom_mod = load_script("generate-r-sbom.py", "r_sbom")
        cls.gov_mod = load_script("generate-r-governance-artifacts.py", "r_governance")
        cls.vuln_mod = load_script("scan-r-vulnerabilities.py", "r_vuln_scan")

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="r-artifacts-"))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_sbom_can_be_generated_from_installed_packages_csv(self):
        installed_csv = self.tmp_dir / "installed-packages.csv"
        with installed_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["package_name", "package_version", "library_path"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "package_name": "dplyr",
                    "package_version": "1.1.4",
                    "library_path": "/tmp/lib",
                }
            )

        out_file = self.tmp_dir / "r-packages.cdx.json"
        import sys

        old_argv = sys.argv
        try:
            sys.argv = [
                "generate-r-sbom.py",
                "--installed-packages-file",
                str(installed_csv),
                "--out-file",
                str(out_file),
            ]
            self.sbom_mod.main()
        finally:
            sys.argv = old_argv

        data = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data["components"]), 1)
        self.assertEqual(data["components"][0]["purl"], "pkg:cran/dplyr@1.1.4")

    def test_governance_prefers_materialized_package_inventory(self):
        run_dir = self.tmp_dir / "run"
        run_dir.mkdir()
        (run_dir / "renv.lock").write_text(
            json.dumps({"Packages": {"foo": {"Version": "1.0.0"}}}),
            encoding="utf-8",
        )
        (run_dir / "installed-packages.csv").write_text(
            "package_name,package_version,library_path\nbar,2.0.0,/tmp/lib\n",
            encoding="utf-8",
        )
        (run_dir / "trivy-sbom-report.json").write_text(
            json.dumps({"Results": []}),
            encoding="utf-8",
        )

        self.gov_mod.main(
            [
                "--run-dir",
                str(run_dir),
                "--platform",
                "linux-amd64",
            ]
        )

        approval_csv = (run_dir / "approval-candidate-packages.csv").read_text(
            encoding="utf-8"
        )
        self.assertIn("bar,2.0.0", approval_csv)
        self.assertNotIn("foo,1.0.0", approval_csv)

    def test_governance_reads_osv_findings(self):
        run_dir = self.tmp_dir / "run"
        run_dir.mkdir()
        (run_dir / "renv.lock").write_text(
            json.dumps({"Packages": {"gh": {"Version": "1.1.0"}}}),
            encoding="utf-8",
        )
        (run_dir / "installed-packages.csv").write_text(
            "package_name,package_version,library_path\n"
            "gh,1.1.0,/tmp/lib\n",
            encoding="utf-8",
        )
        (run_dir / "osv-report.json").write_text(
            json.dumps(
                {
                    "findings": [
                        {
                            "package_name": "gh",
                            "package_version": "1.1.0",
                            "vulnerability_id": "RSEC-2023-001",
                            "aliases": ["CVE-2023-12345"],
                            "severity": "HIGH",
                            "summary": "Example advisory",
                            "reference_url": "https://osv.dev/vulnerability/RSEC-2023-001",
                            "fixed_versions": ["1.4.1"],
                            "fixed_available": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(SystemExit) as exc:
            self.gov_mod.main(
                [
                    "--run-dir",
                    str(run_dir),
                    "--platform",
                    "linux-amd64",
                ]
            )
        self.assertEqual(exc.exception.code, 3)

        findings_csv = (run_dir / "vulnerability-findings.csv").read_text(encoding="utf-8")
        remediation_csv = (run_dir / "remediation-required.csv").read_text(encoding="utf-8")
        self.assertIn("RSEC-2023-001", findings_csv)
        self.assertIn("CVE-2023-12345", findings_csv)
        self.assertIn("1.4.1", remediation_csv)

    def test_vulnerability_scanner_queries_osv_for_cran_packages(self):
        installed_csv = self.tmp_dir / "installed-packages.csv"
        installed_csv.write_text(
            "package_name,package_version,library_path,priority,repository\n"
            "gh,1.1.0,/tmp/lib,,CRAN\n"
            "base,4.4.0,/tmp/lib,base,\n",
            encoding="utf-8",
        )
        lock_file = self.tmp_dir / "renv.lock"
        lock_file.write_text(
            json.dumps(
                {
                    "Packages": {
                        "gh": {"Version": "1.1.0", "Source": "Repository", "Repository": "CRAN"}
                    }
                }
            ),
            encoding="utf-8",
        )
        out_file = self.tmp_dir / "osv-report.json"

        fake_response = {
            "results": [
                {
                    "vulns": [
                        {
                            "id": "RSEC-2024-001",
                            "aliases": ["CVE-2024-99999"],
                            "database_specific": {"severity": "HIGH"},
                            "summary": "Mock advisory",
                            "references": [{"type": "ADVISORY", "url": "https://osv.dev/vulnerability/RSEC-2024-001"}],
                            "affected": [{"ranges": [{"events": [{"introduced": "0"}, {"fixed": "1.4.1"}]}]}],
                        }
                    ]
                }
            ]
        }

        with mock.patch.object(self.vuln_mod, "post_json", return_value=fake_response) as post_json, mock.patch.object(
            self.vuln_mod,
            "fetch_osv_vulnerability",
            return_value=fake_response["results"][0]["vulns"][0],
        ) as fetch_osv_vulnerability:
            self.vuln_mod.main(
                [
                    "--installed-packages-file",
                    str(installed_csv),
                    "--lock-file",
                    str(lock_file),
                    "--out-file",
                    str(out_file),
                ]
            )

        report = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertEqual(len(report["queried_packages"]), 1)
        self.assertEqual(report["queried_packages"][0]["package_name"], "gh")
        self.assertEqual(report["skipped_packages"][0]["package_name"], "base")
        self.assertEqual(report["findings"][0]["vulnerability_id"], "RSEC-2024-001")
        self.assertEqual(report["findings"][0]["fixed_versions"], ["1.4.1"])
        post_json.assert_called_once()
        fetch_osv_vulnerability.assert_called_once()


if __name__ == "__main__":
    unittest.main()
