#!/usr/bin/env python3
"""Convert an R renv.lock into an unpinned requested-package manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


DEFAULT_REPOSITORIES = {
    "RSPM": "https://packagemanager.posit.co/all/latest",
    "CRAN": "https://cloud.r-project.org",
}


def build_requested_entry(name: str, record: dict) -> dict:
    source = record.get("Source", "Repository")
    entry = {
        "name": name,
        "source": source,
    }
    if source == "Repository":
        repository = record.get("Repository")
        if repository:
            entry["repository"] = repository
        return entry

    remote_username = record.get("RemoteUsername") or record.get("Remoteusername")
    remote_repo = record.get("RemoteRepo") or record.get("RemoteRepository")
    remote_ref = record.get("RemoteRef")
    remote_host = record.get("RemoteHost")
    remote_type = record.get("RemoteType")

    if remote_type:
        entry["remote_type"] = remote_type
    if remote_host:
        entry["remote_host"] = remote_host
    if remote_username:
        entry["remote_username"] = remote_username
    if remote_repo:
        entry["remote_repo"] = remote_repo
    if remote_ref:
        entry["remote_ref"] = remote_ref
    if remote_username and remote_repo:
        entry["ref"] = (
            f"{remote_username}/{remote_repo}@{remote_ref}"
            if remote_ref
            else f"{remote_username}/{remote_repo}"
        )
    return entry


def convert_lockfile(lockfile_path: Path) -> dict:
    with lockfile_path.open(encoding="utf-8-sig") as handle:
        lock = json.load(handle)

    packages = lock.get("Packages", {})
    requested_packages = [
        build_requested_entry(name, record)
        for name, record in sorted(packages.items(), key=lambda item: item[0].lower())
    ]

    r_version = lock.get("R", {}).get("Version")
    repositories = lock.get("R", {}).get("Repositories") or DEFAULT_REPOSITORIES
    if isinstance(repositories, list):
        repositories = {
            item["Name"]: item["URL"]
            for item in repositories
            if isinstance(item, dict) and item.get("Name") and item.get("URL")
        }
    if not isinstance(repositories, dict) or not repositories:
        repositories = DEFAULT_REPOSITORIES

    return {
        "schema_version": 1,
        "ecosystem": "r",
        "input_type": "requested-packages",
        "generated_from": "renv.lock",
        "r": {"version": r_version},
        "repositories": repositories,
        "packages": requested_packages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-file", required=True)
    parser.add_argument("--out-file", required=True)
    args = parser.parse_args()

    manifest = convert_lockfile(Path(args.lock_file))
    out_path = Path(args.out_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
