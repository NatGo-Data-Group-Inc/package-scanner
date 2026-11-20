#!/usr/bin/env python3
"""Plan one consolidated R library from a capture of several overlapping ones.

    python3 scripts/plan-r-consolidation.py <capture-dir> --r-version 4.4.0 --out <dir> \
        [--policy newest-on-disk | snapshot --cran-index PACKAGES --snapshot-date 2026-11-20] \
        [--osv-report <capture-dir>/osv-report.json] [--positron-order project,shared,system] \
        [--rstudio-order project,system]

Version policies:
  newest-on-disk  each package at the newest version found in any captured library
  snapshot        each CRAN package at its version in a dated CRAN snapshot (the PACKAGES
                  index from that date), except: recommended packages keep R's own bundled
                  versions; packages whose snapshot version needs a newer R, and packages
                  withdrawn from CRAN, keep the newest version on disk; new dependencies of
                  snapshot versions are added. The lock points at the dated snapshot so a
                  rebuild later gets the same versions.

Reads the files written by capture-r-library.py (r-packages.csv, renv.lock) and
produces:

  renv.lock                   the target: every package available today, at the newest
                              version found in any library, for --r-version (base excluded)
  consolidation-plan.csv      per package: version in each library, what each IDE loads
                              today, the target version, and why it changed
  consolidation-summary.json  counts, dependency-constraint problems, flags

Rule for version one: nobody loses a package that either IDE can load today;
duplicates collapse to the newest copy; everything is rebuilt for one R version.
Trimming unused packages is a separate, later decision.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

LIBRARY_LABELS = (("posit/projects", "project"), ("/efs/", "shared"), ("/opt/R/", "system"), ("/lib/R/library", "system"))


def library_label(path: str) -> str:
    for needle, label in LIBRARY_LABELS:
        if needle in path:
            return label
    return path


def version_key(version: str) -> tuple:
    """R package versions: integers separated by '.' or '-' (package_version semantics)."""
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.-]", version.strip()))


CONSTRAINT = re.compile(r"^\s*([A-Za-z0-9.]+)\s*(?:\(\s*(>=|<=|==|>|<)\s*([0-9][0-9.\-]*)\s*\))?\s*$")


def satisfies(version: str, op: str, wanted: str) -> bool:
    a, b = version_key(version), version_key(wanted)
    width = max(len(a), len(b))
    a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    return {">=": a >= b, ">": a > b, "<=": a <= b, "<": a < b, "==": a == b}[op]


def read_cran_index(path: Path) -> dict[str, dict]:
    """Parse a CRAN src/contrib/PACKAGES file into {package: fields}."""
    index = {}
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        fields = {}
        for line in re.sub(r"\n[ \t]+", " ", block).splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                fields[key.strip()] = value.strip()
        if "Package" in fields and "Version" in fields:
            index[fields["Package"]] = fields
    return index


def dep_specs(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def required_r(fields: dict) -> str | None:
    match = re.search(r"\bR\s*\(\s*>=\s*([0-9][0-9.\-]*)\s*\)", fields.get("Depends", ""))
    return match.group(1) if match else None


def effective(versions_by_label: dict, order: list[str]) -> str:
    for label in order:
        if label in versions_by_label:
            return versions_by_label[label]
    return ""


def plan(capture: Path, r_version: str, positron_order: list[str], rstudio_order: list[str], osv_report: Path | None,
         policy: str = "newest-on-disk", cran: dict | None = None, snapshot_date: str | None = None):
    rows = list(csv.DictReader(open(capture / "r-packages.csv", encoding="utf-8")))
    captured_lock = json.loads((capture / "renv.lock").read_text(encoding="utf-8"))["Packages"]
    vulnerable = defaultdict(list)
    if osv_report and osv_report.exists():
        for f in json.loads(osv_report.read_text(encoding="utf-8")).get("findings", []):
            vulnerable[(f["package_name"], f["package_version"])].append(f.get("vulnerability_id", ""))

    by_package: dict[str, dict] = defaultdict(lambda: {"versions": {}, "rows": []})
    for row in rows:
        entry = by_package[row["package"]]
        label = library_label(row["libpath"])
        entry["versions"].setdefault(label, row["version"])
        entry["rows"].append(row)

    target, plan_rows, flags = {}, [], defaultdict(list)
    for name in sorted(by_package, key=str.lower):
        entry = by_package[name]
        priorities = {r["priority"] for r in entry["rows"]}
        if "base" in priorities:
            continue                                             # ships with R itself
        candidates = sorted(entry["rows"], key=lambda r: version_key(r["version"]), reverse=True)
        best = dict(candidates[0])
        decision = "newest on disk"
        if policy == "snapshot":
            fields = cran.get(name) if cran else None
            if "recommended" in priorities:
                decision = "recommended: R's bundled version"
            elif fields is None:
                decision = "withdrawn from CRAN: newest on disk, from archive"
                flags["withdrawn_from_cran"].append(f"{name} {best['version']}")
            elif required_r(fields) and version_key(required_r(fields)) > version_key(r_version):
                decision = f"held back: snapshot {fields['Version']} needs R >= {required_r(fields)}"
                flags["held_back_needs_newer_r"].append(f"{name} {best['version']} (snapshot {fields['Version']} needs R >= {required_r(fields)})")
            else:
                if version_key(fields["Version"]) != version_key(best["version"]):
                    decision = f"snapshot version (was {best['version']})"
                else:
                    decision = "snapshot version (same as on disk)"
                best["version"] = fields["Version"]
                best["built_r_version"] = r_version
                best["needs_compilation"] = (fields.get("NeedsCompilation") or best["needs_compilation"]).lower()
        source = best["source"]
        repository = best["repository"] or ("Bioconductor" if source == "Bioconductor" else "")
        lock_entry = {"Package": name, "Version": best["version"],
                      "Source": "Repository" if source in ("Repository", "Unknown") else source}
        if source == "Unknown":
            repository = repository or "CRAN"
            flags["source_assumed_cran"].append(name)
        if repository:
            lock_entry["Repository"] = repository
        if source not in ("Repository", "Bioconductor", "Unknown"):
            flags["non_repository_source"].append(f"{name} ({source})")
        captured = captured_lock.get(name)
        if policy == "snapshot" and cran and name in cran and cran[name]["Version"] == best["version"]:
            for field in ("Depends", "Imports", "LinkingTo"):
                if cran[name].get(field):
                    lock_entry[field] = dep_specs(cran[name][field])
            if cran[name].get("NeedsCompilation"):
                lock_entry["NeedsCompilation"] = cran[name]["NeedsCompilation"]
        elif captured and captured.get("Version") == best["version"]:
            for field in ("Depends", "Imports", "LinkingTo", "NeedsCompilation", "Priority",
                          "RemoteType", "RemoteHost", "RemoteRepo", "RemoteUsername", "RemoteRef", "RemoteSha", "RemoteUrl"):
                if field in captured:
                    lock_entry[field] = captured[field]
        target[name] = lock_entry

        versions = entry["versions"]
        pos, rst = effective(versions, positron_order), effective(versions, rstudio_order)
        reasons = []
        if len(set(versions.values())) > 1:
            reasons.append("duplicates collapsed")
        if policy == "snapshot":
            reasons.append(decision)
        if best["built_r_version"][:3] != r_version[:3]:
            reasons.append(f"rebuild (only build on disk is for R {best['built_r_version']})")
        if pos and pos != best["version"]:
            reasons.append(f"Positron upgrade {pos} -> {best['version']}")
        if rst != best["version"]:
            reasons.append("new to RStudio" if not rst else f"RStudio upgrade {rst} -> {best['version']}")
        removed = [f"{v} ({', '.join(vulnerable[(name, v)])})" for v in set(versions.values()) if (name, v) in vulnerable]
        if removed:
            reasons.append("vulnerable version removed: " + "; ".join(removed))
        still = vulnerable.get((name, best["version"]))
        if still:
            flags["target_version_vulnerable"].append(f"{name} {best['version']} ({', '.join(still)})")
        plan_rows.append([name, best["version"], versions.get("project", ""), versions.get("shared", ""), versions.get("system", ""),
                          pos, rst, best["needs_compilation"], "; ".join(reasons) or "unchanged"])

    if policy == "snapshot" and cran:
        # Snapshot versions can need packages nobody had installed: add them from the snapshot.
        queue = list(target)
        while queue:
            name = queue.pop()
            for field in ("Depends", "Imports", "LinkingTo"):
                for spec in target[name].get(field, []):
                    dep = re.split(r"[\s(]", spec, maxsplit=1)[0]
                    if dep in target or dep in BASE or dep == "R" or dep not in cran:
                        continue
                    fields = cran[dep]
                    target[dep] = {"Package": dep, "Version": fields["Version"], "Source": "Repository", "Repository": "CRAN"}
                    for f2 in ("Depends", "Imports", "LinkingTo"):
                        if fields.get(f2):
                            target[dep][f2] = dep_specs(fields[f2])
                    flags["added_dependency"].append(f"{dep} {fields['Version']} (needed by {name})")
                    plan_rows.append([dep, fields["Version"], "", "", "", "", "", (fields.get("NeedsCompilation") or "").lower(),
                                      f"added: new dependency of {name} in the snapshot"])
                    queue.append(dep)

    # Dependency constraints the target must satisfy (from packages whose metadata we have).
    problems = []
    for name, entry in target.items():
        for field in ("Depends", "Imports", "LinkingTo"):
            for spec in entry.get(field, []):
                match = CONSTRAINT.match(spec)
                if not match or match.group(1) == "R":
                    continue
                dep, op, wanted = match.groups()
                if dep in target and op and not satisfies(target[dep]["Version"], op, wanted):
                    problems.append(f"{name} needs {dep} {op} {wanted}, target has {target[dep]['Version']}")
                elif dep not in target and dep not in BASE and op is not None:
                    problems.append(f"{name} needs {dep} {op} {wanted}, not in target")
    missing = sorted({spec.split()[0].split("(")[0] for e in target.values() for f in ("Depends", "Imports", "LinkingTo")
                      for spec in e.get(f, []) if spec.split()[0].split("(")[0] not in target
                      and spec.split()[0].split("(")[0] not in BASE and spec.split()[0].split("(")[0] != "R"})

    repo_url = (f"https://packagemanager.posit.co/cran/{snapshot_date}" if policy == "snapshot" and snapshot_date
                else "https://cloud.r-project.org")
    lock = {"R": {"Version": r_version, "Repositories": [{"Name": "CRAN", "URL": repo_url}]},
            "Packages": dict(sorted(target.items(), key=lambda kv: kv[0].lower()))}
    counts = {
        "packages_on_disk_distinct": len(by_package),
        "target_packages": len(target),
        "base_excluded": sum(1 for e in by_package.values() if "base" in {r["priority"] for r in e["rows"]}),
        "duplicates_collapsed": sum(1 for r in plan_rows if "duplicates collapsed" in r[8]),
        "rebuilt_from_older_r": sum(1 for r in plan_rows if "rebuild" in r[8]),
        "positron_changes": sum(1 for r in plan_rows if r[5] and r[5] != r[1]),
        "new_to_rstudio": sum(1 for r in plan_rows if not r[6]),
        "rstudio_upgrades": sum(1 for r in plan_rows if r[6] and r[6] != r[1]),
        "vulnerable_versions_removed": sum(1 for r in plan_rows if "vulnerable version removed" in r[8]),
        "compiled_packages": sum(1 for r in plan_rows if r[7] == "yes"),
    }
    counts["added_dependencies"] = len(flags.get("added_dependency", []))
    summary = {"r_version": r_version, "policy": policy, "snapshot_date": snapshot_date, "repository": repo_url,
               "positron_order": positron_order, "rstudio_order": rstudio_order,
               "counts": counts, "dependency_constraint_problems": problems, "dependencies_missing_from_target": missing,
               "flags": {k: sorted(v) for k, v in flags.items()}}
    return lock, plan_rows, summary


BASE = {"base", "compiler", "datasets", "graphics", "grDevices", "grid", "methods", "parallel", "splines",
        "stats", "stats4", "tcltk", "tools", "utils"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Plan one consolidated R library from a capture-r-library.py capture.")
    ap.add_argument("capture", type=Path)
    ap.add_argument("--r-version", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--osv-report", type=Path)
    ap.add_argument("--positron-order", default="project,shared,system")
    ap.add_argument("--rstudio-order", default="project,system")
    ap.add_argument("--policy", choices=("newest-on-disk", "snapshot"), default="newest-on-disk")
    ap.add_argument("--cran-index", type=Path, help="CRAN src/contrib/PACKAGES file for the snapshot date (snapshot policy)")
    ap.add_argument("--snapshot-date", help="Snapshot date, e.g. 2026-11-20 (snapshot policy)")
    args = ap.parse_args(argv)
    if args.policy == "snapshot" and not (args.cran_index and args.snapshot_date):
        ap.error("--policy snapshot needs --cran-index and --snapshot-date")
    osv = args.osv_report or (args.capture / "osv-report.json")
    cran = read_cran_index(args.cran_index) if args.cran_index else None
    lock, rows, summary = plan(args.capture, args.r_version, args.positron_order.split(","), args.rstudio_order.split(","),
                               osv if osv.exists() else None, args.policy, cran, args.snapshot_date)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "renv.lock").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with open(args.out / "consolidation-plan.csv", "w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["package", "target_version", "project", "shared", "system", "positron_loads_today",
                    "rstudio_loads_today", "needs_compilation", "change"])
        w.writerows(rows)
    (args.out / "consolidation-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    c = summary["counts"]
    print(f"target: {c['target_packages']} packages for R {args.r_version} "
          f"({c['packages_on_disk_distinct']} distinct on disk, {c['base_excluded']} base excluded)")
    print(f"duplicates collapsed: {c['duplicates_collapsed']}; rebuilt from older R: {c['rebuilt_from_older_r']}; "
          f"compiled: {c['compiled_packages']}")
    print(f"Positron: {c['positron_changes']} version changes; RStudio: {c['new_to_rstudio']} new, {c['rstudio_upgrades']} upgrades")
    print(f"vulnerable versions removed: {c['vulnerable_versions_removed']}; added dependencies: {c['added_dependencies']}")
    print(f"repository: {summary['repository']}")
    for key in ("dependency_constraint_problems", "dependencies_missing_from_target"):
        if summary[key]:
            print(f"{key}: {len(summary[key])}: " + "; ".join(summary[key][:8]))
    for key, items in summary["flags"].items():
        print(f"flag {key}: " + ", ".join(items[:10]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
