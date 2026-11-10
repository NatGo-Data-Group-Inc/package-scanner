"""--pin-versions: a candidate captured from an existing environment must reproduce it.

Without pins, build-candidate-from-env-artifacts.py emitted bare package names
and silently left core runtime packages (openssl, zlib, libgcc, ...) to the
solver, so a scan reported on the newest versions rather than the environment
that is actually installed.
"""
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-candidate-from-env-artifacts.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_candidate_pin_versions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


builder = load_builder()

INVENTORY = [
    {"name": "python", "version": "3.11.13", "channel": "conda-forge"},
    {"name": "pip", "version": "25.2", "channel": "conda-forge"},
    {"name": "openssl", "version": "3.5.4", "channel": "conda-forge"},
    {"name": "libzlib", "version": "1.3.1", "channel": "conda-forge"},
    {"name": "setuptools", "version": "80.9.0", "channel": "conda-forge"},
    {"name": "numpy", "version": "1.26.4", "channel": "conda-forge"},
    {"name": "pytorch", "version": "2.6.0", "channel": "conda-forge"},
    {"name": "utf8proc", "version": "2.6.1", "channel": "pkgs/main"},
    {"name": "dbt-core", "version": "0.21.0", "channel": "pypi"},
]
REQUIREMENTS = "numpy==1.26.4\ntorch==2.6.0\ndbt-core==0.21.0\nextra-dist==1.0\n"


class PinVersions(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="candidate-pin-"))
        (self.tmp / "environment.yml").write_text(
            "name: base\nchannels:\n  - conda-forge\n  - defaults\ndependencies: []\n", encoding="utf-8"
        )
        (self.tmp / "conda-list.json").write_text(json.dumps(INVENTORY), encoding="utf-8")
        (self.tmp / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, pin):
        # Probe answers: numpy is on conda; pytorch is not, so it moves to pip as torch.
        def probe(**kwargs):
            return kwargs["name"] != "pytorch"

        with mock.patch.object(builder, "conda_has_package", side_effect=probe):
            return builder.build_candidate_payload(
                "test-env",
                self.tmp / "environment.yml",
                self.tmp / "conda-list.json",
                self.tmp / "requirements.txt",
                conda_bin="conda",
                prefer_conda_available=True,
                target_subdir="linux-64",
                pin_versions=pin,
            )

    @staticmethod
    def split(payload):
        conda = [d for d in payload["dependencies"] if isinstance(d, str)]
        pip = next((d["pip"] for d in payload["dependencies"] if isinstance(d, dict)), [])
        return conda, pip

    def test_without_the_flag_behaviour_is_unchanged(self):
        conda, _ = self.split(self.build(pin=False))
        self.assertIn("python", conda)
        self.assertNotIn("openssl", conda)

    def test_every_entry_is_pinned_to_the_installed_version(self):
        conda, pip = self.split(self.build(pin=True))
        for entry in conda:
            self.assertRegex(entry, r"^[^=\s]+==[^=\s]+$")
        for entry in pip:
            self.assertRegex(entry, r"^[^=\s]+==[^=\s]+$")
        self.assertIn("python==3.11.13", conda)
        self.assertIn("numpy==1.26.4", conda)
        self.assertIn("utf8proc==2.6.1", conda)
        self.assertIn("torch==2.6.0", pip)
        self.assertIn("dbt-core==0.21.0", pip)
        self.assertIn("extra-dist==1.0", pip)

    def test_core_packages_the_split_omits_are_restored(self):
        conda, _ = self.split(self.build(pin=True))
        for entry in ("openssl==3.5.4", "libzlib==1.3.1", "setuptools==80.9.0", "pip==25.2"):
            self.assertIn(entry, conda)

    def test_exact_versions_without_build_strings(self):
        # conda's single '=' is a prefix match (=0.4 accepts 0.4.2); '==' is exact.
        conda, _ = self.split(self.build(pin=True))
        self.assertFalse([c for c in conda if c.count("=") != 2])

    def test_every_installed_package_is_represented_once(self):
        conda, pip = self.split(self.build(pin=True))
        names = [re.split(r"==?", e)[0].lower() for e in conda + pip]
        self.assertEqual(len(names), len(set(names)))
        for row in INVENTORY:
            expected = "torch" if row["name"] == "pytorch" else row["name"]
            self.assertIn(expected, names)

    def test_unpinnable_pip_entry_is_an_error(self):
        (self.tmp / "requirements.txt").write_text("numpy==1.26.4\nmystery\n", encoding="utf-8")
        payload = {"name": "x", "channels": [], "dependencies": ["numpy", {"pip": ["mystery"]}]}
        with self.assertRaises(builder.PinError):
            builder.pin_payload(payload, INVENTORY, self.tmp / "requirements.txt")

    def test_command_line_flag_and_error_exit(self):
        output = self.tmp / "candidate.yml"
        base = [sys.executable, str(SCRIPT), "--name", "cli-env", "--output", str(output),
                "--environment-yml", str(self.tmp / "environment.yml"),
                "--conda-list-json", str(self.tmp / "conda-list.json"),
                "--requirements-txt", str(self.tmp / "requirements.txt")]
        result = subprocess.run(base + ["--pin-versions"], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = yaml.safe_load(output.read_text())
        self.assertIn("openssl==3.5.4", payload["dependencies"])

        # A requirement with no version anywhere cannot be pinned: refuse, do not emit it bare.
        (self.tmp / "requirements.txt").write_text(REQUIREMENTS + "mystery\n", encoding="utf-8")
        bad = subprocess.run(base + ["--pin-versions"], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(bad.returncode, 2, bad.stderr)
        self.assertIn("candidate-pin-error: cannot pin: pip:mystery", bad.stderr)
        self.assertNotIn("Traceback", bad.stderr)

    def test_micromamba_2_conda_list_shape_is_accepted(self):
        (self.tmp / "conda-list.json").write_text(
            json.dumps({"log_history": [], "packages": INVENTORY}), encoding="utf-8"
        )
        self.assertEqual(len(builder.load_conda_inventory(self.tmp / "conda-list.json")), len(INVENTORY))


if __name__ == "__main__":
    unittest.main()
