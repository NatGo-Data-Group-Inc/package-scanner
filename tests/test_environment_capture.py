"""capture-environment.py (runs on the host, e.g. the MIP) and receive-environment-capture.py (runs here)."""
import ast
import base64
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "scripts" / "capture-environment.py"
RECEIVE = ROOT / "scripts" / "receive-environment-capture.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load(CAPTURE, "capture_environment")
receive = load(RECEIVE, "receive_environment_capture")


def make_env(root: Path) -> Path:
    """A minimal conda environment on disk: conda packages, a pip-only package, an egg-info."""
    prefix = root / "env"
    meta = prefix / "conda-meta"
    site = prefix / "lib" / "python3.11" / "site-packages"
    meta.mkdir(parents=True)
    site.mkdir(parents=True)

    def conda(name, version, build, channel, depends=(), files=()):
        (meta / f"{name}-{version}-{build}.json").write_text(json.dumps({
            "name": name, "version": version, "build": build, "build_number": 0,
            "channel": channel, "subdir": "linux-64", "depends": list(depends),
            "requested_spec": name, "files": list(files),
        }))

    def dist(dirname, name, version, egg=False):
        d = site / dirname
        d.mkdir()
        (d / ("PKG-INFO" if egg else "METADATA")).write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")

    conda("python", "3.11.13", "h1_cpython", "https://conda.anaconda.org/conda-forge", ["openssl >=3"])
    conda("openssl", "3.5.4", "h2", "https://conda.anaconda.org/conda-forge")
    conda("pytorch", "2.6.0", "cpu_h3", "https://conda.anaconda.org/conda-forge", ["python >=3.11"],
          ["lib/python3.11/site-packages/torch-2.6.0.dist-info/METADATA"])
    conda("utf8proc", "2.6.1", "h4", "https://repo.anaconda.com/pkgs/main")
    conda("pip", "25.2", "pyh5", "https://conda.anaconda.org/conda-forge", [],
          ["lib/python3.11/site-packages/pip-25.2.dist-info/METADATA"])
    dist("torch-2.6.0.dist-info", "torch", "2.6.0")
    dist("pip-25.2.dist-info", "pip", "25.2")
    dist("dbt_core-0.21.0.dist-info", "dbt-core", "0.21.0")          # pip-installed: no conda owner
    dist("old_thing-1.0-py3.11.egg-info", "old-thing", "1.0", egg=True)
    (meta / "history").write_text("==> 2025-10-01 00:00:00 <==\n# cmd: mamba install lifelines\n")
    return prefix


def add_stray_site_packages(prefix: Path):
    """The 0326 layout: a duplicate in a mistyped lib/python3.1 and a py313 package in lib/python3.13."""
    for minor, dirname, name, version in [("3.1", "dbt_core-0.21.0.dist-info", "dbt-core", "0.21.0"),
                                          ("3.13", "mmhash3-3.0.1.dist-info", "mmhash3", "3.0.1")]:
        d = prefix / "lib" / f"python{minor}" / "site-packages" / dirname
        d.mkdir(parents=True)
        (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")


class Capture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="capture-"))
        self.prefix = make_env(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parses_as_python_3_6_for_rhel8_platform_python(self):
        ast.parse(CAPTURE.read_text(), feature_version=(3, 6))

    def test_inventory_matches_what_is_on_disk(self):
        files, info = capture.build_capture(str(self.prefix), "test-env")
        conda_list = json.loads(files["conda-list.json"])
        by_name = {p["name"]: p for p in conda_list}
        self.assertEqual(by_name["openssl"]["channel"], "conda-forge")
        self.assertEqual(by_name["utf8proc"]["channel"], "pkgs/main")
        self.assertEqual(by_name["dbt-core"]["channel"], "pypi")
        self.assertNotIn("torch", by_name)              # owned by the pytorch conda package
        self.assertEqual(info["counts"], {"conda_packages": 5, "pip_only_packages": 2, "python_distributions": 4,
                                          "not_importable_distributions": 0,
                                          "channels": {"conda-forge": 4, "pkgs/main": 1}})
        self.assertEqual(info["warnings"], [])

    def test_stray_site_packages_are_flagged_and_pip_only_is_deduplicated(self):
        add_stray_site_packages(self.prefix)
        files, info = capture.build_capture(str(self.prefix), "test-env")
        pip_only = [p["name"] for p in json.loads(files["conda-list.json"]) if p["channel"] == "pypi"]
        self.assertEqual(sorted(pip_only), ["dbt-core", "mmhash3", "old-thing"])   # dbt-core once, not twice
        self.assertEqual(info["counts"]["not_importable_distributions"], 2)
        self.assertEqual(len(info["warnings"]), 2)
        self.assertTrue(any("lib/python3.13/site-packages: 1 distribution(s) not importable by python 3.11: mmhash3" in w
                            for w in info["warnings"]))
        self.assertTrue(any("lib/python3.1/site-packages" in w and "dbt-core" in w for w in info["warnings"]))
        rows = json.loads(files["python-distributions.json"])
        stray = {(r["name"], r["site_packages"]) for r in rows if not r["importable"]}
        self.assertEqual(stray, {("dbt-core", "lib/python3.1/site-packages"), ("mmhash3", "lib/python3.13/site-packages")})
        self.assertIn("mmhash3==3.0.1", files["requirements.txt"])   # still inventoried: it is on disk

    def test_requirements_include_every_distribution_with_pip_itself(self):
        files, _ = capture.build_capture(str(self.prefix), "test-env")
        self.assertEqual(files["requirements.txt"].splitlines(),
                         ["dbt-core==0.21.0", "old-thing==1.0", "pip==25.2", "torch==2.6.0"])

    def test_environment_yml_lists_channels_by_use_and_exact_builds(self):
        files, _ = capture.build_capture(str(self.prefix), "test-env")
        env = files["environment.yml"].splitlines()
        self.assertEqual(env[:4], ["name: test-env", "channels:", "  - conda-forge", "  - pkgs/main"])
        self.assertIn("  - openssl=3.5.4=h2", env)
        self.assertIn("    - dbt-core==0.21.0", env)

    def test_dependency_graph_and_history_are_kept(self):
        files, _ = capture.build_capture(str(self.prefix), "test-env")
        graph = {r["name"]: r for r in json.loads(files["conda-meta-records.json"])}
        self.assertEqual(graph["python"]["depends"], ["openssl >=3"])
        self.assertIn("mamba install lifelines", files["history"])

    def test_not_a_conda_environment_is_a_clear_error(self):
        result = subprocess.run([sys.executable, str(CAPTURE), str(self.tmp), "--name", "x"],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no conda-meta records", result.stderr)

    def test_archive_is_deterministic(self):
        files, _ = capture.build_capture(str(self.prefix), "test-env")
        files.pop("capture-info.json")  # holds the capture time
        a, b = self.tmp / "a.tar.gz", self.tmp / "b.tar.gz"
        capture.write_archive(files, a)
        capture.write_archive(files, b)
        self.assertEqual(a.read_bytes(), b.read_bytes())


class RoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="roundtrip-"))
        prefix = make_env(self.tmp)
        result = subprocess.run([sys.executable, str(CAPTURE), str(prefix), "--name", "rt", "--output-dir", str(self.tmp)],
                                capture_output=True, text=True, check=True)
        self.md5 = next(l.split()[1] for l in result.stdout.splitlines() if l.startswith("md5 "))
        self.stdout = result.stdout
        self.b64 = self.tmp / "rt-capture.b64"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_host_output_gives_md5_and_chunk_commands(self):
        self.assertRegex(self.md5, r"^[0-9a-f]{32}$")
        self.assertRegex(self.stdout, r"clear; sed -n '1,\d+p' rt-capture\.b64")
        lines = len(self.b64.read_text().splitlines())
        self.assertIn(f"{lines} lines in rt-capture.b64", self.stdout)

    def test_receive_verifies_and_unpacks(self):
        out = self.tmp / "out"
        result = subprocess.run([sys.executable, str(RECEIVE), str(self.b64), "--md5", self.md5, "--out", str(out)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified rt", result.stdout)
        self.assertIn("--pin-versions", result.stdout)
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["capture-info.json", "conda-list.json", "conda-meta-records.json",
                          "environment.yml", "history", "python-distributions.json", "requirements.txt"])

    def test_missing_chunk_is_rejected(self):
        lines = self.b64.read_text().splitlines()
        self.b64.write_text("\n".join(lines[:-1]) + "\n")
        result = subprocess.run([sys.executable, str(RECEIVE), str(self.b64), "--md5", self.md5, "--out", str(self.tmp / "o")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("receive-error", result.stderr)

    def test_unsafe_archive_member_is_rejected(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            member = tarfile.TarInfo("../escape.txt")
            member.size = 1
            tar.addfile(member, io.BytesIO(b"x"))
        blob = raw.getvalue()
        bad = self.tmp / "bad.b64"
        bad.write_text(base64.b64encode(blob).decode())
        with self.assertRaises(receive.ReceiveError):
            receive.receive(bad, hashlib.md5(blob).hexdigest(), self.tmp / "o")


if __name__ == "__main__":
    unittest.main()
