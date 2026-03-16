#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path


def load_renv_packages(lock_path: Path):
    data = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    packages = data.get("Packages")
    if not isinstance(packages, dict):
        raise ValueError("renv.lock missing Packages object")

    out = []
    for name, details in packages.items():
        if not isinstance(details, dict):
            continue
        version = str(details.get("Version", "")).strip()
        if not version:
            continue
        out.append({"name": name, "version": version})
    if not out:
        raise ValueError("No R packages with versions found in renv.lock")
    return out


def load_installed_packages(csv_path: Path):
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        out = []
        for row in reader:
            name = str(row.get("package_name") or row.get("Package") or "").strip()
            version = str(row.get("package_version") or row.get("Version") or "").strip()
            if not name or not version:
                continue
            out.append({"name": name, "version": version})
    if not out:
        raise ValueError("No installed R packages with versions found in installed-packages.csv")
    return out


def to_cyclonedx(packages):
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "r-environment"}},
        "components": [
            {
                "type": "library",
                "name": p["name"],
                "version": p["version"],
                "purl": f"pkg:cran/{p['name']}@{p['version']}",
            }
            for p in sorted(packages, key=lambda x: x["name"].lower())
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock-file")
    ap.add_argument("--installed-packages-file")
    ap.add_argument("--out-file", required=True)
    args = ap.parse_args()

    out_path = Path(args.out_file)

    if bool(args.lock_file) == bool(args.installed_packages_file):
        raise SystemExit("Pass exactly one of --lock-file or --installed-packages-file")

    if args.lock_file:
        lock_path = Path(args.lock_file)
        if not lock_path.exists():
            raise SystemExit(f"renv.lock not found: {lock_path}")
        packages = load_renv_packages(lock_path)
    else:
        installed_path = Path(args.installed_packages_file)
        if not installed_path.exists():
            raise SystemExit(f"installed-packages.csv not found: {installed_path}")
        packages = load_installed_packages(installed_path)

    sbom = to_cyclonedx(packages)
    out_path.write_text(json.dumps(sbom, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
