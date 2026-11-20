"""plan-r-consolidation.py: overlapping captured R libraries -> one target renv.lock and a reviewable plan."""
import csv
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "plan-r-consolidation.py"
spec = importlib.util.spec_from_file_location("plan_r_consolidation", SCRIPT)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)

PROJECT = "/opt/posit/projects/p/renv/library/R-4.4/x86_64-pc-linux-gnu"
SHARED = "/efs/di/packages/R/x86_64-pc-linux-gnu-library/4.1"
SYSTEM = "/opt/R/4.4.0/lib/R/library"

ROWS = [
    # package, version, libpath, effective, priority, source, repository, compiled, built_r
    ("rlang", "1.2.0", PROJECT, "yes", "", "Repository", "CRAN", "yes", "4.4.0"),
    ("rlang", "1.1.1", SHARED, "no (shadowed)", "", "Repository", "CRAN", "yes", "4.1.2"),
    ("gh", "1.4.0", SHARED, "yes", "", "Repository", "CRAN", "no", "4.1.2"),         # shared only, vulnerable
    ("Matrix", "1.6-1", SHARED, "yes", "recommended", "Repository", "CRAN", "yes", "4.1.2"),
    ("Matrix", "1.7-0", SYSTEM, "no (shadowed)", "recommended", "Repository", "CRAN", "yes", "4.4.0"),
    ("maptools", "1.1-8", SHARED, "yes", "", "Repository", "CRAN", "yes", "4.1.2"),   # withdrawn
    ("arules", "1.7-6", SHARED, "yes", "", "Repository", "CRAN", "yes", "4.1.2"),     # snapshot needs newer R
    ("stats", "4.4.0", SYSTEM, "yes", "base", "Unknown", "", "", "4.4.0"),
]
CRAN_INDEX = """Package: rlang
Version: 1.2.1
Depends: R (>= 3.5.0)
NeedsCompilation: yes

Package: gh
Version: 1.5.0
Imports: cli (>= 3.0.1), httr2 (>= 1.0.0)
NeedsCompilation: no

Package: cli
Version: 3.6.5
NeedsCompilation: yes

Package: httr2
Version: 1.2.1
Imports: cli, rlang (>= 1.1.0)
NeedsCompilation: no

Package: Matrix
Version: 1.7-6
Depends: R (>= 4.7)

Package: arules
Version: 1.7.15
Depends: R (>= 4.5.0), Matrix (>= 1.4-0)
"""


def write_capture(root: Path) -> Path:
    cap = root / "capture"
    cap.mkdir()
    with open(cap / "r-packages.csv", "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["package", "version", "libpath", "effective", "priority", "source", "repository",
                    "needs_compilation", "built_r_version", "built_platform", "built_date"])
        for r in ROWS:
            w.writerow([*r, "x86_64-pc-linux-gnu", "2024-01-01"])
    lock = {"R": {"Version": "4.4.0"}, "Packages": {
        "rlang": {"Package": "rlang", "Version": "1.2.0", "Source": "Repository"},
        "gh": {"Package": "gh", "Version": "1.4.0", "Source": "Repository", "Imports": ["cli (>= 3.0.1)"]},
    }}
    (cap / "renv.lock").write_text(json.dumps(lock))
    (cap / "osv-report.json").write_text(json.dumps({"findings": [
        {"package_name": "gh", "package_version": "1.4.0", "vulnerability_id": "RSEC-2025-0"}]}))
    (root / "PACKAGES").write_text(CRAN_INDEX)
    return cap


class NewestOnDisk(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plan-"))
        cap = write_capture(self.tmp)
        self.lock, rows, self.summary = planner.plan(cap, "4.4.0", ["project", "shared", "system"], ["project", "system"],
                                                     cap / "osv-report.json")
        self.rows = {r[0]: r for r in rows}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_newest_copy_wins_and_base_is_excluded(self):
        pkgs = self.lock["Packages"]
        self.assertEqual(pkgs["rlang"]["Version"], "1.2.0")
        self.assertEqual(pkgs["Matrix"]["Version"], "1.7-0")
        self.assertNotIn("stats", pkgs)

    def test_ide_views_are_reported(self):
        self.assertEqual(self.rows["gh"][5], "1.4.0")            # Positron loads the shared copy
        self.assertEqual(self.rows["gh"][6], "")                 # RStudio cannot load it at all
        self.assertIn("new to RStudio", self.rows["gh"][8])
        self.assertIn("Positron upgrade 1.6-1 -> 1.7-0", self.rows["Matrix"][8])

    def test_shared_only_packages_are_marked_for_rebuild(self):
        self.assertIn("rebuild (only build on disk is for R 4.1.2)", self.rows["gh"][8])


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plan-"))
        cap = write_capture(self.tmp)
        cran = planner.read_cran_index(self.tmp / "PACKAGES")
        self.lock, rows, self.summary = planner.plan(cap, "4.4.0", ["project", "shared", "system"], ["project", "system"],
                                                     cap / "osv-report.json", "snapshot", cran, "2026-11-20")
        self.rows = {r[0]: r for r in rows}
        self.pkgs = self.lock["Packages"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cran_packages_take_the_snapshot_version(self):
        self.assertEqual(self.pkgs["rlang"]["Version"], "1.2.1")
        self.assertEqual(self.pkgs["gh"]["Version"], "1.5.0")

    def test_vulnerable_version_is_replaced(self):
        self.assertIn("vulnerable version removed: 1.4.0 (RSEC-2025-0)", self.rows["gh"][8])
        self.assertEqual(self.summary["counts"]["vulnerable_versions_removed"], 1)

    def test_recommended_keep_rs_bundled_version(self):
        self.assertEqual(self.pkgs["Matrix"]["Version"], "1.7-0")
        self.assertIn("recommended: R's bundled version", self.rows["Matrix"][8])

    def test_needs_newer_r_is_held_back(self):
        self.assertEqual(self.pkgs["arules"]["Version"], "1.7-6")
        self.assertTrue(any(f.startswith("arules 1.7-6") for f in self.summary["flags"]["held_back_needs_newer_r"]))

    def test_withdrawn_packages_keep_the_disk_version(self):
        self.assertEqual(self.pkgs["maptools"]["Version"], "1.1-8")
        self.assertEqual(self.summary["flags"]["withdrawn_from_cran"], ["maptools 1.1-8"])

    def test_new_dependencies_are_added_transitively(self):
        self.assertEqual(self.pkgs["httr2"]["Version"], "1.2.1")   # new in gh 1.5.0
        self.assertEqual(self.pkgs["cli"]["Version"], "3.6.5")     # needed by gh and httr2
        self.assertEqual(self.summary["counts"]["added_dependencies"], 2)

    def test_lock_points_at_the_dated_snapshot(self):
        self.assertEqual(self.lock["R"]["Repositories"][0]["URL"], "https://packagemanager.posit.co/cran/2026-11-20")

    def test_dependency_constraints_are_checked(self):
        self.assertEqual(self.summary["dependency_constraint_problems"], [])
        self.pkgs["httr2"]["Imports"] = ["rlang (>= 9.0.0)"]
        self.assertFalse(planner.satisfies(self.pkgs["rlang"]["Version"], ">=", "9.0.0"))


class CommandLine(unittest.TestCase):
    def test_snapshot_requires_index_and_date(self):
        tmp = Path(tempfile.mkdtemp(prefix="plancli-"))
        try:
            cap = write_capture(tmp)
            bad = subprocess.run([sys.executable, str(SCRIPT), str(cap), "--r-version", "4.4.0", "--out", str(tmp / "o"),
                                  "--policy", "snapshot"], capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)
            ok = subprocess.run([sys.executable, str(SCRIPT), str(cap), "--r-version", "4.4.0", "--out", str(tmp / "o"),
                                 "--policy", "snapshot", "--cran-index", str(tmp / "PACKAGES"), "--snapshot-date", "2026-11-20"],
                                capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            for name in ("renv.lock", "consolidation-plan.csv", "consolidation-summary.json"):
                self.assertTrue((tmp / "o" / name).exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
