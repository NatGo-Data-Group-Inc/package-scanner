import csv
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
