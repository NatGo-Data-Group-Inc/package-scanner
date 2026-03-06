#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def load_renv_packages(lock_path: Path):
    data = json.loads(lock_path.read_text(encoding="utf-8"))
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
    ap.add_argument("--lock-file", required=True)
    ap.add_argument("--out-file", required=True)
    args = ap.parse_args()

    lock_path = Path(args.lock_file)
    out_path = Path(args.out_file)
    if not lock_path.exists():
        raise SystemExit(f"renv.lock not found: {lock_path}")

    packages = load_renv_packages(lock_path)
    sbom = to_cyclonedx(packages)
    out_path.write_text(json.dumps(sbom, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
