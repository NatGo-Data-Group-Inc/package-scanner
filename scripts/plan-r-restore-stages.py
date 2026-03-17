#!/usr/bin/env python3
"""Plan staged R package restore batches from an renv.lock file."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict, deque
from pathlib import Path


DEPENDENCY_FIELDS = ("Depends", "Imports", "LinkingTo")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.]*")


def parse_dependency_name(entry: str) -> str | None:
    match = NAME_RE.match((entry or "").strip())
    if not match:
        return None
    name = match.group(0)
    if name == "R":
        return None
    return name


def package_dependencies(record: dict, known_packages: set[str]) -> list[str]:
    deps: list[str] = []
    for field in DEPENDENCY_FIELDS:
        for entry in record.get(field, []):
            name = parse_dependency_name(entry)
            if name and name in known_packages:
                deps.append(name)
    return sorted(set(deps), key=str.lower)


def topo_sort(packages: dict[str, dict]) -> list[str]:
    known = set(packages)
    deps = {name: package_dependencies(record, known) for name, record in packages.items()}
    dependents: dict[str, set[str]] = defaultdict(set)
    indegree = {name: 0 for name in packages}

    for name, package_deps in deps.items():
        indegree[name] = len(package_deps)
        for dep in package_deps:
            dependents[dep].add(name)

    ready = deque(sorted((name for name, degree in indegree.items() if degree == 0), key=str.lower))
    ordered: list[str] = []
    while ready:
        current = ready.popleft()
        ordered.append(current)
        for dependent in sorted(dependents.get(current, ()), key=str.lower):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    if len(ordered) != len(packages):
        remaining = sorted((name for name in packages if name not in ordered), key=str.lower)
        ordered.extend(remaining)
    return ordered


def build_stage_plan(lockfile: Path, stage_package_count: int) -> dict:
    data = json.loads(lockfile.read_text(encoding="utf-8-sig"))
    packages = data.get("Packages", {})
    ordered = topo_sort(packages)
    stages = []
    for start in range(0, len(ordered), stage_package_count):
        chunk = ordered[start:start + stage_package_count]
        stage_index = len(stages) + 1
        stages.append(
            {
                "stage_index": str(stage_index),
                "packages": chunk,
                "packages_json": json.dumps(chunk),
                "package_count": len(chunk),
                "final_stage": "false",
            }
        )
    if not stages:
        stages.append(
            {
                "stage_index": "1",
                "packages": [],
                "packages_json": "[]",
                "package_count": 0,
                "final_stage": "true",
            }
        )
    else:
        stages[-1]["final_stage"] = "true"
    for stage in stages:
        stage["total_stages"] = str(len(stages))
    return {
        "package_count": len(ordered),
        "stage_count": len(stages),
        "stages": stages,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock-file", required=True)
    ap.add_argument("--stage-package-count", type=int, default=25)
    ap.add_argument("--out-file", default="")
    args = ap.parse_args()

    plan = build_stage_plan(Path(args.lock_file), args.stage_package_count)
    payload = json.dumps(plan, indent=2)
    print(payload)
    if args.out_file:
        Path(args.out_file).write_text(payload + "\n", encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
