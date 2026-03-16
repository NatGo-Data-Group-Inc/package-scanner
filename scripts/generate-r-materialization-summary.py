#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def count_installed_packages(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return sum(1 for _ in reader)


def file_record(path: Path):
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--r-version", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--library-path", required=True)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    installed_csv = run_dir / "installed-packages.csv"

    bundles = sorted(run_dir.glob("renv-*.tar.gz"))
    checksums = sorted(run_dir.glob("renv-*.tar.gz.sha256"))

    summary = {
        "platform": args.platform,
        "ecosystem": "r",
        "r_version": args.r_version,
        "cache_dir": args.cache_dir,
        "library_path": args.library_path,
        "counts": {
            "restored_packages": count_installed_packages(installed_csv),
        },
        "artifacts": {
            "installed_packages_csv": file_record(installed_csv),
            "session_info": file_record(run_dir / "session-info.txt"),
            "restore_log": file_record(run_dir / "restore.log"),
            "renv_status": file_record(run_dir / "renv-status.txt"),
            "bundles": [file_record(path) for path in bundles],
            "checksums": [file_record(path) for path in checksums],
        },
    }

    (run_dir / "materialization-summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
