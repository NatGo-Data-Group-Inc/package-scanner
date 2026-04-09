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


class RequestedPackagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_script("convert-r-lockfile-to-requested.py", "convert_r_lockfile_to_requested")

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="r-requested-"))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_conversion_drops_pinned_versions_for_repository_packages(self):
        lockfile = self.tmp_dir / "renv.lock"
        lockfile.write_text(
            json.dumps(
                {
                    "R": {"Version": "4.4.0"},
                    "Packages": {
                        "ggeffects": {
                            "Version": "1.1.2",
                            "Source": "Repository",
                            "Repository": "CRAN",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        manifest = self.mod.convert_lockfile(lockfile)

        self.assertEqual(manifest["input_type"], "requested-packages")
        self.assertEqual(manifest["r"]["version"], "4.4.0")
        self.assertEqual(manifest["packages"], [{"name": "ggeffects", "source": "Repository", "repository": "CRAN"}])

    def test_conversion_preserves_github_request_without_sha_pin(self):
        lockfile = self.tmp_dir / "renv.lock"
        lockfile.write_text(
            json.dumps(
                {
                    "R": {"Version": "4.4.0"},
                    "Packages": {
                        "ROhdsiWebApi": {
                            "Version": "2.0.0",
                            "Source": "GitHub",
                            "RemoteType": "github",
                            "RemoteUsername": "OHDSI",
                            "RemoteRepo": "ROhdsiWebApi",
                            "RemoteRef": "main",
                            "RemoteSha": "abcdef123456",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        manifest = self.mod.convert_lockfile(lockfile)

        self.assertEqual(
            manifest["packages"],
            [
                {
                    "name": "ROhdsiWebApi",
                    "source": "GitHub",
                    "remote_type": "github",
                    "remote_username": "OHDSI",
                    "remote_repo": "ROhdsiWebApi",
                    "remote_ref": "main",
                    "ref": "OHDSI/ROhdsiWebApi@main",
                }
            ],
        )
