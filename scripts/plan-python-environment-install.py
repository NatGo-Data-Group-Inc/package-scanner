#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from package_scanner.python_dependency_classification import classify_dependency, package_name


def write_env(path: Path, name: str, channels: list[str], specs: list[str]) -> None:
    payload = {"name": name, "channels": channels, "dependencies": specs}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-file", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()

    env_path = Path(args.environment_file)
    run_dir = Path(args.run_dir)
    data = yaml.safe_load(env_path.read_text(encoding="utf-8"))

    channels = list(data.get("channels", []))
    dependencies = list(data.get("dependencies", []))
    name = str(data.get("name", "target"))

    conda_specs: list[str] = []
    pip_specs: list[str] = []
    for item in dependencies:
        if isinstance(item, str):
            conda_specs.append(item)
        elif isinstance(item, dict) and "pip" in item:
            pip_specs.extend(str(spec) for spec in item["pip"])

    core_specs: list[str] = []
    native_specs: list[str] = []
    python_specs: list[str] = []

    for spec in conda_specs:
        classification = classify_dependency(spec)
        if classification == "core":
            core_specs.append(spec)
        elif classification == "native":
            native_specs.append(spec)
        else:
            python_specs.append(spec)

    write_env(run_dir / "environment.conda-core.yml", name, channels, core_specs)
    write_env(run_dir / "environment.conda-native.yml", name, channels, native_specs)
    write_env(run_dir / "environment.conda-python.yml", name, channels, python_specs)
    (run_dir / "environment.pip.requirements.txt").write_text(
        "\n".join(pip_specs) + ("\n" if pip_specs else ""),
        encoding="utf-8",
    )
    (run_dir / "environment.install-plan.json").write_text(
        json.dumps(
            {
                "conda_core_count": len(core_specs),
                "conda_native_count": len(native_specs),
                "conda_python_count": len(python_specs),
                "pip_count": len(pip_specs),
                "conda_core": [package_name(spec) for spec in core_specs],
                "conda_native_sample": [package_name(spec) for spec in native_specs[:40]],
                "conda_python_sample": [package_name(spec) for spec in python_specs[:40]],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
