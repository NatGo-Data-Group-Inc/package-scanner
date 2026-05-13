#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
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


NAME_RE = re.compile(r"[=<>!~\s]")


def normalize_name(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace(".", "-")


def package_name(spec: str) -> str:
    return normalize_name(NAME_RE.split(spec.strip(), maxsplit=1)[0])


def load_requested_packages(environment_path: Path) -> tuple[list[str], list[str]]:
    if not environment_path.exists():
        return [], []
    try:
        import yaml

        data = yaml.safe_load(environment_path.read_text(encoding="utf-8"))
    except Exception:
        return [], []
    dependencies = list((data or {}).get("dependencies", []))
    conda_requested: list[str] = []
    pip_requested: list[str] = []
    for item in dependencies:
        if isinstance(item, str):
            conda_requested.append(package_name(item))
        elif isinstance(item, dict) and "pip" in item:
            for spec in item.get("pip", []):
                pip_requested.append(package_name(str(spec)))
    return sorted(set(conda_requested)), sorted(set(pip_requested))


def load_conda_package_names(conda_list_path: Path) -> list[str]:
    if not conda_list_path.exists():
        return []
    try:
        data = json.loads(conda_list_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    names = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(normalize_name(name))
    return sorted(set(names))


def load_pip_package_names(requirements_path: Path) -> list[str]:
    if not requirements_path.exists():
        return []
    names = []
    for raw in requirements_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            continue
        names.append(package_name(line))
    return sorted(set(names))


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
    requested_conda_packages, requested_pip_packages = load_requested_packages(environment_path)
    realized_conda_packages = load_conda_package_names(conda_list_path)
    realized_pip_packages = load_pip_package_names(requirements_path)
    missing_requested_conda_packages = sorted(
        pkg for pkg in requested_conda_packages if pkg not in realized_conda_packages
    )
    missing_requested_pip_packages = sorted(
        pkg for pkg in requested_pip_packages if pkg not in realized_pip_packages
    )
    summary = {
        "platform": args.platform,
        "python_version": args.python_version,
        "requirements_lock_sha256": sha256_text(requirements_path) if requirements_path.exists() else "",
        "environment_yml_sha256": sha256_text(environment_path) if environment_path.exists() else "",
        "pip_package_count": count_packages(requirements_path),
        "conda_package_count": load_conda_packages(conda_list_path),
        "requested_conda_package_count": len(requested_conda_packages),
        "requested_pip_package_count": len(requested_pip_packages),
        "missing_requested_conda_package_count": len(missing_requested_conda_packages),
        "missing_requested_pip_package_count": len(missing_requested_pip_packages),
        "requested_conda_packages": requested_conda_packages,
        "requested_pip_packages": requested_pip_packages,
        "missing_requested_conda_packages": missing_requested_conda_packages,
        "missing_requested_pip_packages": missing_requested_pip_packages,
        "root_prefix": args.root_prefix,
        "env_prefix": args.env_prefix,
    }
    (run_dir / "materialization-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
