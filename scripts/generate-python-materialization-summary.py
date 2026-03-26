#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def count_packages(requirements_path: Path) -> int:
    if not requirements_path.exists():
        return 0
    count = 0
    with requirements_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                count += 1
    return count


def load_conda_packages(conda_list_path: Path) -> int:
    if not conda_list_path.exists():
        return 0
    try:
        data = json.loads(conda_list_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if isinstance(data, list):
        return len(data)
    return 0


def sha256_text(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--root-prefix", required=True)
    parser.add_argument("--env-prefix", required=True)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    requirements_path = run_dir / "requirements.lock.txt"
    conda_list_path = run_dir / "conda-list.json"
    environment_path = run_dir / "environment.yml"
    summary = {
        "platform": args.platform,
        "python_version": args.python_version,
        "requirements_lock_sha256": sha256_text(requirements_path) if requirements_path.exists() else "",
        "environment_yml_sha256": sha256_text(environment_path) if environment_path.exists() else "",
        "pip_package_count": count_packages(requirements_path),
        "conda_package_count": load_conda_packages(conda_list_path),
        "root_prefix": args.root_prefix,
        "env_prefix": args.env_prefix,
    }
    (run_dir / "materialization-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
