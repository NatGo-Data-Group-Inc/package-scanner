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


class SummaryInputError(Exception):
    """An input file exists but cannot be read as expected.

    Raised instead of returning an empty result: an empty inventory or an empty
    request list makes the validator's answer meaningless (it reports good runs
    as missing everything, or bad runs as missing nothing).
    """


def read_conda_list(conda_list_path: Path) -> list[dict]:
    """Return the package records from `micromamba list --json` output.

    Older micromamba writes a bare list of records. Newer micromamba (2.x)
    writes {"log_history": [...], "packages": [...]}. Both are accepted.
    A missing file means no Conda inventory was produced (scan-only paths).
    """
    if not conda_list_path.exists():
        return []
    try:
        data = json.loads(conda_list_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SummaryInputError(f"{conda_list_path.name} is not valid JSON: {error}") from error
    if isinstance(data, dict) and isinstance(data.get("packages"), list):
        data = data["packages"]
    if not isinstance(data, list):
        shape = sorted(data) if isinstance(data, dict) else type(data).__name__
        raise SummaryInputError(
            f"{conda_list_path.name} has an unrecognised shape ({shape}); expected a list of "
            "package records or an object with a 'packages' list"
        )
    return [item for item in data if isinstance(item, dict)]


def load_conda_packages(conda_list_path: Path) -> int:
    return len(read_conda_list(conda_list_path))


NAME_RE = re.compile(r"[=<>!~\s]")

PACKAGE_ALIASES = {
    "pyyaml": {"yaml"},
    "yaml": {"pyyaml"},
    "pytorch": {"torch"},
    "torch": {"pytorch"},
    "scikit-learn": {"sklearn"},
    "sklearn": {"scikit-learn"},
}


def normalize_name(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace(".", "-")


def package_name(spec: str) -> str:
    return normalize_name(NAME_RE.split(spec.strip(), maxsplit=1)[0])


def equivalent_package_names(name: str) -> set[str]:
    normalized = normalize_name(name)
    return {normalized, *PACKAGE_ALIASES.get(normalized, set())}


def is_package_satisfied(requested_name: str, realized_names: set[str]) -> bool:
    return any(name in realized_names for name in equivalent_package_names(requested_name))


def load_requested_packages(environment_path: Path) -> tuple[list[str], list[str]]:
    if not environment_path.exists():
        raise SummaryInputError(f"{environment_path.name} not found; cannot tell what was requested")
    try:
        import yaml
    except ImportError as error:
        raise SummaryInputError("PyYAML is not installed; cannot read the environment file") from error
    try:
        data = yaml.safe_load(environment_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise SummaryInputError(f"{environment_path.name} cannot be parsed: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("dependencies"), list):
        raise SummaryInputError(f"{environment_path.name} has no 'dependencies' list")
    dependencies = data["dependencies"]
    conda_requested: list[str] = []
    pip_requested: list[str] = []
    for item in dependencies:
        if isinstance(item, str):
            conda_requested.append(package_name(item))
        elif isinstance(item, dict) and "pip" in item:
            for spec in item.get("pip", []):
                pip_requested.append(package_name(str(spec)))
    return sorted(set(conda_requested)), sorted(set(pip_requested))


def load_requested_requirements(requirements_path: Path) -> list[str]:
    if not requirements_path.exists():
        return []
    requested: list[str] = []
    for raw in requirements_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        requested.append(package_name(line))
    return sorted(set(requested))


def load_conda_package_names(conda_list_path: Path) -> list[str]:
    names = []
    for item in read_conda_list(conda_list_path):
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
    parser.add_argument("--input-type", default="environment-yaml")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    requirements_path = run_dir / "requirements.lock.txt"
    conda_list_path = run_dir / "conda-list.json"
    environment_path = run_dir / "environment.yml"
    if args.input_type in {"requirements-lock", "requested-packages"}:
        requested_conda_packages = []
        requested_pip_packages = load_requested_requirements(requirements_path)
    else:
        requested_conda_packages, requested_pip_packages = load_requested_packages(environment_path)
    realized_conda_packages = load_conda_package_names(conda_list_path)
    realized_pip_packages = load_pip_package_names(requirements_path)
    realized_package_names = set(realized_conda_packages) | set(realized_pip_packages)
    missing_requested_conda_packages = sorted(
        pkg for pkg in requested_conda_packages if not is_package_satisfied(pkg, realized_package_names)
    )
    missing_requested_pip_packages = sorted(
        pkg for pkg in requested_pip_packages if not is_package_satisfied(pkg, realized_package_names)
    )
    summary = {
        "platform": args.platform,
        "python_version": args.python_version,
        "input_type": args.input_type,
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
    try:
        raise SystemExit(main())
    except SummaryInputError as error:
        import sys

        print(f"materialization-summary-error: {error}", file=sys.stderr)
        raise SystemExit(5)
