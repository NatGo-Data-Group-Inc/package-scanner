"""capture-r-library.py: installed R libraries -> renv.lock, inventory and R-version build skew."""
import ast
import csv
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "scripts" / "capture-r-library.py"
RECEIVE = ROOT / "scripts" / "receive-environment-capture.py"

spec = importlib.util.spec_from_file_location("capture_r_library", CAPTURE)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def package(lib: Path, name: str, version: str, built_r: str = "4.4.0", compiled: str = "no", **extra):
    d = lib / name
    d.mkdir(parents=True)
    fields = {"Package": name, "Version": version, "NeedsCompilation": compiled,
              "Built": f"R {built_r}; x86_64-pc-linux-gnu; 2024-01-01 00:00:00 UTC; unix", **extra}
    (d / "DESCRIPTION").write_text("".join(f"{k}: {v}\n" for k, v in fields.items() if v is not None))


def make_libraries(root: Path):
    """A project library over an older shared library, like the Posit layout."""
    project, shared, system = root / "project", root / "shared", root / "system"
    package(project, "rlang", "1.1.7", compiled="yes", Repository="CRAN")
    package(project, "dplyr", "1.1.4", compiled="yes", Repository="CRAN",
            Imports="rlang (>= 1.1.7),\n    vctrs (>= 0.6.4), glue")
    package(project, "arrow", "17.0.0", compiled="yes", Repository="CRAN")
    package(shared, "rlang", "1.1.1", built_r="4.1.3", compiled="yes", Repository="CRAN")   # shadowed
    package(shared, "vctrs", "0.6.5", built_r="4.1.3", compiled="yes", Repository="CRAN")   # compiled, built for 4.1
    package(shared, "glue", "1.7.0", built_r="4.1.3", compiled="no", Repository="CRAN")     # pure R, built for 4.1
    package(shared, "aws.s3", "0.3.22", built_r="4.1.3", Repository="CRAN", Imports="httr, xml2")
    package(shared, "limma", "3.58.1", built_r="4.1.3", compiled="yes", biocViews="Software")
    package(shared, "mytool", "0.1.0", RemoteType="github", RemoteUsername="someone", RemoteRepo="mytool", RemoteSha="abc123")
    package(system, "stats", "4.4.0", Priority="base", compiled=None)
    package(system, "MASS", "7.3-60", Priority="recommended", Repository="CRAN", compiled="yes")
    (system / "translations").mkdir()
    (system / "translations" / "DESCRIPTION").write_text("Package: translations\nVersion: 4.4.0\n")
    return [project, shared, system]


class CaptureR(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rcap-"))
        self.libs = make_libraries(self.tmp)
        self.files, self.info = capture.build_capture([str(p) for p in self.libs], "posit-test", "4.4.0")
        self.lock = json.loads(self.files["renv.lock"])
        self.rows = list(csv.DictReader(io.StringIO(self.files["r-packages.csv"])))
        self.skew = list(csv.DictReader(io.StringIO(self.files["build-skew.csv"])))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parses_as_python_3_6_for_rhel8_platform_python(self):
        ast.parse(CAPTURE.read_text(), feature_version=(3, 6))

    def test_first_library_wins_and_shadowed_copies_are_recorded(self):
        self.assertEqual(self.lock["Packages"]["rlang"]["Version"], "1.1.7")
        rlang = {(r["libpath"].split("/")[-1], r["effective"]) for r in self.rows if r["package"] == "rlang"}
        self.assertEqual(rlang, {("project", "yes"), ("shared", "no (shadowed)")})
        self.assertEqual(self.info["counts"]["shadowed_copies"], 1)

    def test_build_skew_marks_compiled_packages_high(self):
        risk = {r["package"]: r["risk"] for r in self.skew}
        self.assertEqual(risk["vctrs"], "HIGH")
        self.assertEqual(risk["limma"], "HIGH")
        self.assertEqual(risk["glue"], "low")
        self.assertNotIn("rlang", risk)          # the effective rlang is the 4.4 build; the 4.1 copy is shadowed
        self.assertEqual(self.skew[0]["risk"], "HIGH")   # HIGH rows first
        self.assertEqual(self.info["counts"]["build_skew_compiled"], 2)

    def test_lock_sources_and_base_exclusion(self):
        pkgs = self.lock["Packages"]
        self.assertEqual(self.lock["R"]["Version"], "4.4.0")
        self.assertNotIn("stats", pkgs)                       # base ships with R
        self.assertEqual(pkgs["MASS"]["Source"], "Repository")
        self.assertEqual(pkgs["limma"]["Source"], "Bioconductor")
        self.assertEqual(pkgs["mytool"]["Source"], "Github")
        self.assertEqual(pkgs["mytool"]["RemoteSha"], "abc123")
        self.assertEqual(pkgs["dplyr"]["Imports"], ["rlang (>= 1.1.7)", "vctrs (>= 0.6.4)", "glue"])

    def test_scan_input_lists_every_distinct_version_on_disk(self):
        rows = list(csv.DictReader(io.StringIO(self.files["installed-packages.csv"])))
        versions = {(r["Package"], r["Version"]) for r in rows}
        self.assertIn(("rlang", "1.1.7"), versions)
        self.assertIn(("rlang", "1.1.1"), versions)      # shadowed here, but loaded by a session without the project library
        self.assertEqual(next(r["Repository"] for r in rows if r["Package"] == "limma"), "Bioconductor")
        self.assertEqual(len(rows), self.info["counts"]["distinct_package_versions"])

    def test_non_packages_are_skipped(self):
        self.assertNotIn("translations", {r["package"] for r in self.rows})

    def test_missing_dependencies_are_warned(self):
        self.assertTrue(any("httr" in w and "xml2" in w for w in self.info["warnings"]))

    def test_no_packages_is_an_error(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        result = subprocess.run([sys.executable, str(CAPTURE), str(empty), "--name", "x", "--r-version", "4.4.0"],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no R packages found", result.stderr)

    def test_round_trip_through_the_receiver(self):
        out = self.tmp / "out"
        result = subprocess.run([sys.executable, str(CAPTURE), *map(str, self.libs), "--name", "rt", "--r-version", "4.4.0",
                                 "--output-dir", str(self.tmp)], capture_output=True, text=True, check=True)
        self.assertIn("build skew vs R 4.4.0: 4 packages, 2 with compiled code", result.stdout)
        md5 = next(l.split()[1] for l in result.stdout.splitlines() if l.startswith("md5 "))
        received = subprocess.run([sys.executable, str(RECEIVE), str(self.tmp / "rt-capture.b64"), "--md5", md5, "--out", str(out)],
                                  capture_output=True, text=True)
        self.assertEqual(received.returncode, 0, received.stderr)
        self.assertIn("verified rt", received.stdout)
        self.assertIn("build skew 4 (2 with compiled code)", received.stdout)
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["build-skew.csv", "capture-info.json", "installed-packages.csv", "r-packages.csv", "renv.lock"])


if __name__ == "__main__":
    unittest.main()
