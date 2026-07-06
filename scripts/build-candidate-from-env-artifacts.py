#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from package_scanner.python_dependency_classification import (
    CORE_NAMES,
    classify_dependency,
    normalize_name,
    pip_requirement_name,
    should_include_pip_requirement,
)

SKIP_CONDA = {
    *CORE_NAMES,
    "pywin32-on-windows",
}

REQ_NAME_RE = re.compile(r"^([A-Za-z0-9_.-]+)")


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
        payload = json.loads(proc.stdout)
    except Exception:
        return None
    return bool(payload.get(name))


def resolve_inputs_from_zip(zip_path: Path) -> tuple[Path, Path, Path]:
    with tempfile.TemporaryDirectory(prefix="candidate-artifacts-") as tmpdir:
        extract_dir = Path(tmpdir)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_dir)
        environment_files = sorted(extract_dir.rglob("*.environment.yml")) + sorted(extract_dir.rglob("environment.yml"))
        conda_list_files = sorted(extract_dir.rglob("*.conda-list.json")) + sorted(extract_dir.rglob("conda-list.json"))
        requirements_files = sorted(extract_dir.rglob("*.requirements.txt")) + sorted(extract_dir.rglob("requirements.txt"))
        if not environment_files or not conda_list_files or not requirements_files:
            raise FileNotFoundError(
                "ZIP must contain environment.yml, conda-list.json, and requirements.txt artifacts."
            )
        env_copy = Path(tempfile.mkstemp(prefix="candidate-env-", suffix=".yml")[1])
        conda_copy = Path(tempfile.mkstemp(prefix="candidate-conda-", suffix=".json")[1])
        req_copy = Path(tempfile.mkstemp(prefix="candidate-req-", suffix=".txt")[1])
        env_copy.write_bytes(environment_files[0].read_bytes())
        conda_copy.write_bytes(conda_list_files[0].read_bytes())
        req_copy.write_bytes(requirements_files[0].read_bytes())
        return env_copy, conda_copy, req_copy


def default_python_bin(env_prefix: Path) -> Path:
    posix = env_prefix / "bin" / "python"
    if posix.exists():
        return posix
    windows = env_prefix / "python.exe"
    if windows.exists():
        return windows
    windows_scripts = env_prefix / "Scripts" / "python.exe"
    if windows_scripts.exists():
        return windows_scripts
    return posix


def run_capture_command(command: list[str], output_path: Path) -> None:
    proc = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        stdout = proc.stdout.strip()
        detail = stderr or stdout or f"exit code {proc.returncode}"
        raise RuntimeError(f"Command failed: {' '.join(command)}: {detail}")
    output_path.write_text(proc.stdout, encoding="utf-8")


def capture_inputs_from_environment(
    *,
    name: str,
    env_prefix: Path,
    artifacts_dir: Path,
    conda_bin: str,
    python_bin: str | None,
    artifacts_zip: Path | None,
) -> tuple[Path, Path, Path]:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stem = name
    environment_path = artifacts_dir / f"{stem}.environment.yml"
    conda_list_path = artifacts_dir / f"{stem}.conda-list.json"
    requirements_path = artifacts_dir / f"{stem}.requirements.txt"

    resolved_python = Path(python_bin) if python_bin else default_python_bin(env_prefix)

    run_capture_command(
        [conda_bin, "env", "export", "-p", str(env_prefix)],
        environment_path,
    )
    run_capture_command(
        [conda_bin, "list", "-p", str(env_prefix), "--json"],
        conda_list_path,
    )
    run_capture_command(
        [str(resolved_python), "-m", "pip", "freeze"],
        requirements_path,
    )

    if artifacts_zip is not None:
        artifacts_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(artifacts_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(environment_path, arcname=environment_path.name)
            archive.write(conda_list_path, arcname=conda_list_path.name)
            archive.write(requirements_path, arcname=requirements_path.name)

    return environment_path, conda_list_path, requirements_path


def load_channels(environment_path: Path) -> list[str]:
    data = yaml.safe_load(environment_path.read_text(encoding="utf-8")) or {}
    channels = []
    for channel in data.get("channels", []):
        value = str(channel).strip()
        if value and value not in channels:
            channels.append(value)
    return channels


def load_conda_inventory(conda_list_path: Path) -> list[dict]:
    data = json.loads(conda_list_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{conda_list_path} does not contain a conda package list")
    return [row for row in data if isinstance(row, dict)]


def split_conda_and_pip(
    conda_inventory: list[dict],
    *,
    channels: list[str],
    conda_bin: str,
    prefer_conda_available: bool,
    target_subdir: str | None,
) -> tuple[list[str], list[str], set[str], set[str]]:
    conda_names: list[str] = []
    pip_names: list[str] = []
    conda_norm: set[str] = set()
    pip_norm: set[str] = set()
    for row in conda_inventory:
        name = str(row.get("name") or "").strip()
        channel = str(row.get("channel") or "").strip().lower()
        if not name:
            continue
        normalized = normalize_name(name)
        if channel == "pypi":
            pip_name = pip_requirement_name(name)
            pip_normalized = normalize_name(pip_name)
            if (
                normalized not in conda_norm
                and pip_normalized not in pip_norm
                and should_include_pip_requirement(pip_name)
            ):
                pip_norm.add(pip_normalized)
                pip_names.append(pip_name)
            continue
        if normalized in SKIP_CONDA or normalized in conda_norm:
            continue
        if classify_dependency(name) == "python":
            if prefer_conda_available:
                availability = conda_has_package(
                    conda_bin=conda_bin,
                    channels=channels,
                    name=name,
                    target_subdir=target_subdir,
                )
                if availability is not False:
                    conda_norm.add(normalized)
                    conda_names.append(name)
                    continue
            pip_name = pip_requirement_name(name)
            pip_normalized = normalize_name(pip_name)
            if pip_normalized not in pip_norm and should_include_pip_requirement(pip_name):
                pip_norm.add(pip_normalized)
                pip_names.append(pip_name)
            continue
        conda_norm.add(normalized)
        conda_names.append(name)
    return conda_names, pip_names, conda_norm, pip_norm


def collect_additional_pip_requirements(
    requirements_path: Path,
    conda_norm: set[str],
    pip_norm: set[str],
) -> list[str]:
    pip_names: list[str] = []
    for raw in requirements_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = REQ_NAME_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        normalized = normalize_name(name)
        pip_name = pip_requirement_name(name)
        pip_normalized = normalize_name(pip_name)
        if pip_normalized in conda_norm or pip_normalized in pip_norm or not should_include_pip_requirement(pip_name):
            continue
        pip_norm.add(pip_normalized)
        pip_names.append(pip_name)
    return pip_names


def build_candidate_payload(
    name: str,
    environment_path: Path,
    conda_list_path: Path,
    requirements_path: Path,
    *,
    conda_bin: str,
    prefer_conda_available: bool,
    target_subdir: str | None,
) -> dict:
    channels = load_channels(environment_path)
    conda_inventory = load_conda_inventory(conda_list_path)
    conda_names, pip_names, conda_norm, pip_norm = split_conda_and_pip(
        conda_inventory,
        channels=channels,
        conda_bin=conda_bin,
        prefer_conda_available=prefer_conda_available,
        target_subdir=target_subdir,
    )
    pip_names.extend(collect_additional_pip_requirements(requirements_path, conda_norm, pip_norm))
    payload = {
        "name": name,
        "channels": channels,
        "dependencies": [
            "python",
            "pip",
            *unique_ordered(conda_names),
            {"pip": unique_ordered(pip_names)},
        ],
    }
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Candidate name to emit.")
    parser.add_argument("--output", required=True, help="Path to write the candidate YAML.")
    parser.add_argument("--zip", dest="zip_path", help="ZIP containing environment.yml, conda-list.json, and requirements.txt.")
    parser.add_argument("--environment-yml", help="Path to exported conda environment.yml.")
    parser.add_argument("--conda-list-json", help="Path to conda list --json output.")
    parser.add_argument("--requirements-txt", help="Path to pip freeze requirements.txt.")
    parser.add_argument("--env-prefix", help="Existing environment prefix to inspect with conda and pip.")
    parser.add_argument("--conda-bin", default="conda", help="Conda executable to use with --env-prefix.")
    parser.add_argument("--python-bin", help="Python executable to use with --env-prefix. Defaults to the env's python.")
    parser.add_argument("--artifacts-dir", help="Directory to write captured environment artifacts in --env-prefix mode.")
    parser.add_argument("--artifacts-zip", help="Optional ZIP path to package captured artifacts in --env-prefix mode.")
    parser.add_argument(
        "--target-subdir",
        help="Conda target subdir to probe when using --prefer-conda-available, for example linux-64.",
    )
    parser.add_argument(
        "--prefer-conda-available",
        action="store_true",
        help="Keep Python-level conda packages in conda when the package is available in the configured channels.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = Path(args.output)
    direct_mode = any([args.environment_yml, args.conda_list_json, args.requirements_txt])
    zip_mode = bool(args.zip_path)
    capture_mode = bool(args.env_prefix)
    selected_modes = sum([zip_mode, direct_mode, capture_mode])
    if selected_modes != 1:
        raise SystemExit(
            "Choose exactly one input mode: --zip, the three direct artifact files, or --env-prefix."
        )
    if zip_mode:
        environment_path, conda_list_path, requirements_path = resolve_inputs_from_zip(Path(args.zip_path))
    elif capture_mode:
        artifacts_dir = Path(args.artifacts_dir) if args.artifacts_dir else Path(tempfile.mkdtemp(prefix="candidate-capture-"))
        artifacts_zip = Path(args.artifacts_zip) if args.artifacts_zip else None
        environment_path, conda_list_path, requirements_path = capture_inputs_from_environment(
            name=args.name,
            env_prefix=Path(args.env_prefix),
            artifacts_dir=artifacts_dir,
            conda_bin=args.conda_bin,
            python_bin=args.python_bin,
            artifacts_zip=artifacts_zip,
        )
    else:
        if not (args.environment_yml and args.conda_list_json and args.requirements_txt):
            raise SystemExit(
                "Direct mode requires --environment-yml, --conda-list-json, and --requirements-txt."
            )
        environment_path = Path(args.environment_yml)
        conda_list_path = Path(args.conda_list_json)
        requirements_path = Path(args.requirements_txt)
    payload = build_candidate_payload(
        args.name,
        environment_path,
        conda_list_path,
        requirements_path,
        conda_bin=args.conda_bin,
        prefer_conda_available=args.prefer_conda_available,
        target_subdir=args.target_subdir,
    )
    output_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
