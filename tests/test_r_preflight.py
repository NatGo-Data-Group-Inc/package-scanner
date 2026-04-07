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


class RPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_script("preflight-r-native-deps.py", "r_preflight")

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="r-preflight-"))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def write_lockfile(self, packages: dict[str, str]) -> Path:
        lockfile = self.tmp_dir / "renv.lock"
        lockfile.write_text(
            json.dumps(
                {
                    "R": {"Version": "4.4.0"},
                    "Packages": {name: {"Version": version} for name, version in packages.items()},
                }
            ),
            encoding="utf-8",
        )
        return lockfile

    def test_preflight_fails_when_required_native_tool_is_missing(self):
        lockfile = self.write_lockfile({"terra": "1.8-54"})
        with mock.patch.object(self.mod.shutil, "which", side_effect=lambda name: None if name == "gdal-config" else f"/usr/bin/{name}"):
            report = self.mod.build_report(lockfile, "linux-amd64")

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["missing_requirements"][0]["id"], "geospatial-stack")
        missing_labels = {item["label"] for item in report["missing_requirements"][0]["missing_checks"]}
        self.assertIn("gdal-config", missing_labels)

    def test_preflight_passes_when_curated_requirements_are_present(self):
        lockfile = self.write_lockfile({"gmp": "0.7-5", "terra": "1.8-54", "nloptr": "2.2.1"})

        def fake_which(name):
            return f"/usr/bin/{name}"

        with mock.patch.object(self.mod.shutil, "which", side_effect=fake_which), mock.patch.object(
            self.mod.os.path,
            "exists",
            side_effect=lambda path: path == "/usr/include/gmp.h",
        ):
            report = self.mod.build_report(lockfile, "linux-amd64")

        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["missing_requirements"]), 0)

    def test_preflight_flags_missing_mpfr_header(self):
        lockfile = self.write_lockfile({"Rmpfr": "1.1-2"})
        with mock.patch.object(self.mod.os.path, "exists", return_value=False):
            report = self.mod.build_report(lockfile, "linux-amd64")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["missing_requirements"][0]["id"], "mpfr")

    def test_preflight_flags_missing_libuv_header(self):
        lockfile = self.write_lockfile({"fs": "1.6.4"})
        with mock.patch.object(self.mod.os.path, "exists", return_value=False):
            report = self.mod.build_report(lockfile, "linux-amd64")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["missing_requirements"][0]["id"], "libuv")


if __name__ == "__main__":
    unittest.main()
