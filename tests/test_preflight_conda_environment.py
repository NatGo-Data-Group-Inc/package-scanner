import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_script():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "preflight_conda_environment", root / "scripts" / "preflight-conda-environment.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class PreflightCondaEnvironmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_script()

    def test_resolved_packages_prefers_fetch_records(self):
        packages = self.mod.resolved_packages(
            {"actions": {"FETCH": [{"name": "numpy", "version": "2.0.0", "build": "py311_0", "channel": "conda-forge"}]}}
        )
        self.assertEqual(packages, [{"name": "numpy", "version": "2.0.0", "build": "py311_0", "channel": "conda-forge", "url": ""}])

    def test_gate_blocks_high_and_allows_medium(self):
        result = self.mod.gate([
            {"severity": "MEDIUM"},
            {"severity": "HIGH"},
            {"severity": "UNKNOWN"},
        ])
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["blocking_findings"], 1)
        self.assertEqual(result["findings_by_severity"]["MEDIUM"], 1)

    def test_cyclonedx_sbom_preserves_conda_build_identity(self):
        sbom = self.mod.cyclonedx_sbom(
            [{"name": "libxml2", "version": "2.12.7", "build": "h232c23b_1", "channel": "conda-forge", "url": "https://example.invalid/libxml2.conda"}],
            "linux-64",
            "example",
        )
        component = sbom["components"][0]
        self.assertEqual(component["purl"], "pkg:conda/libxml2@2.12.7?build=h232c23b_1&channel=conda-forge")
        self.assertEqual(component["externalReferences"][0]["type"], "distribution")

    def test_explicit_conda_lock_uses_resolved_artifact_urls(self):
        content = self.mod.explicit_conda_lock([
            {"url": "https://repo.example/linux-64/numpy-2.0.0-py311_0.conda"},
        ])
        self.assertEqual(content, "@EXPLICIT\nhttps://repo.example/linux-64/numpy-2.0.0-py311_0.conda\n")

    def test_inventory_difference_detects_a_version_change(self):
        expected = [{"name": "numpy", "version": "2.0.0", "build": "py311_0"}]
        installed = [{"name": "numpy", "version": "2.1.0", "build": "py311_0"}]
        difference = self.mod.inventory_difference(expected, installed)
        self.assertEqual(difference["missing_from_installed"], expected)
        self.assertEqual(difference["unexpected_in_installed"], installed)

    def test_installed_packages_accepts_micromamba_json_envelope(self):
        completed = mock.Mock(
            returncode=0,
            stdout='{"packages": [{"name": "numpy", "version": "2.0.0", "build_string": "py311_0"}]}',
            stderr="",
        )
        with mock.patch.object(self.mod.shutil, "which", return_value="/bin/micromamba"), mock.patch.object(self.mod.subprocess, "run", return_value=completed), mock.patch.object(Path, "is_dir", return_value=True):
            packages = self.mod.installed_packages("micromamba", Path("/tmp/env"))
        self.assertEqual(packages[0]["build"], "py311_0")

    def test_osv_moderate_maps_to_medium(self):
        self.assertEqual(
            self.mod.severity({"database_specific": {"severity": "MODERATE"}}),
            "MEDIUM",
        )

    def test_dry_solve_reports_solver_log_error(self):
        completed = mock.Mock(
            returncode=1,
            stdout='{"log_history": [{"message": "channel unavailable"}]}',
            stderr="",
        )
        with mock.patch.object(self.mod.shutil, "which", return_value="/bin/conda"), mock.patch.object(self.mod.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(self.mod.PreflightError, "channel unavailable"):
                self.mod.dry_solve("conda", Path("candidate.yml"), "linux-64")

    def test_main_writes_evidence_and_returns_pass_for_medium(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_file = root / "candidate.yml"
            env_file.write_text("name: example\ndependencies:\n  - python\n", encoding="utf-8")
            out_dir = root / "out"
            plan = {"actions": {"FETCH": [{"name": "numpy", "version": "2.0.0", "build": "py311_0", "url": "https://repo.example/linux-64/numpy-2.0.0-py311_0.conda"}]}}
            report = {"scanner": "osv", "findings": [{"severity": "MEDIUM"}]}
            with mock.patch.object(self.mod, "dry_solve", return_value=plan), mock.patch.object(self.mod, "osv_assessment", return_value=report):
                code = self.mod.main(["--environment-file", str(env_file), "--out-dir", str(out_dir), "--skip-trivy"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads((out_dir / "package-validation-summary.json").read_text())["package_count"], 1)
            self.assertTrue((out_dir / "conda-dry-run.json").exists())
            self.assertTrue((out_dir / "conda-resolved.cdx.json").exists())


if __name__ == "__main__":
    unittest.main()
