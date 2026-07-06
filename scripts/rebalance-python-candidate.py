#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from package_scanner.python_dependency_classification import (
    classify_dependency,
    normalize_name,
    package_name,
    pip_requirement_name,
    should_include_pip_requirement,
)


def unique_ordered(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        key = normalize_name(item)
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered


def conda_has_package(
    *,
    conda_bin: str,
    channels: list[str],
    name: str,
    target_subdir: str | None,
) -> bool | None:
    cmd = [conda_bin, "search", "--override-channels"]
    for channel in channels:
        cmd.extend(["-c", channel])
    if target_subdir:
        cmd.extend(["--subdir", target_subdir])
    cmd.extend(["--json", name])
    env = {"CONDA_PKGS_DIRS": "/tmp/package-scanner-conda-pkgs", **__import__("os").environ}
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = __import__("json").loads(proc.stdout)
    except Exception:
        return None
    return bool(payload.get(name))


def main() -> int:
    parser = argparse.ArgumentParser(description="Move Python-level conda dependencies into the pip section.")
    parser.add_argument("--input", required=True, help="Source candidate YAML.")
    parser.add_argument("--output", required=True, help="Destination candidate YAML.")
    parser.add_argument("--conda-bin", default="conda", help="Conda executable for availability probes.")
    parser.add_argument(
        "--target-subdir",
        help="Conda target subdir to probe when using --prefer-conda-available, for example linux-64.",
    )
    parser.add_argument(
        "--prefer-conda-available",
        action="store_true",
        help="Keep Python-level conda specs in conda when the package is available in the configured channels.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    data = yaml.safe_load(input_path.read_text(encoding="utf-8")) or {}
    channels = list(data.get("channels", []))

    conda_specs: list[str] = []
    pip_specs: list[str] = []
    for item in data.get("dependencies", []):
        if isinstance(item, str):
            conda_specs.append(item)
        elif isinstance(item, dict) and "pip" in item:
            pip_specs.extend(str(spec) for spec in item.get("pip", []))

    kept_conda: list[str] = []
    moved_to_pip: list[str] = []
    for spec in conda_specs:
        if classify_dependency(spec) == "python":
            if args.prefer_conda_available:
                availability = conda_has_package(
                    conda_bin=args.conda_bin,
                    channels=channels,
                    name=package_name(spec),
                    target_subdir=args.target_subdir,
                )
                if availability is not False:
                    kept_conda.append(spec)
                    continue
            pip_name = pip_requirement_name(spec)
            if should_include_pip_requirement(pip_name):
                moved_to_pip.append(pip_name)
        else:
            kept_conda.append(spec)

    filtered_pip_specs = [spec for spec in pip_specs if should_include_pip_requirement(spec)]

    payload = {
        "name": data.get("name", input_path.stem),
        "channels": channels,
        "dependencies": [
            *unique_ordered(kept_conda),
            {"pip": unique_ordered([*moved_to_pip, *filtered_pip_specs])},
        ],
    }
    output_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
