#!/usr/bin/env python3
"""Dry-resolve a Conda environment and assess the proposed package set.

This command deliberately never creates an environment and never asks Conda to
download package archives.  It only downloads channel metadata needed by the
solver, then writes the resolved plan and OSV vulnerability evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib import request

import yaml


OSV_QUERY_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULNERABILITY_URL = "https://api.osv.dev/v1/vulns"
BLOCKING_SEVERITIES = {"CRITICAL", "HIGH"}
SEVERITY_ALIASES = {
    "MODERATE": "MEDIUM",
    "IMPORTANT": "HIGH",
    "NEGLIGIBLE": "LOW",
    "UNSPECIFIED": "UNKNOWN",
}


class PackageValidationError(RuntimeError):
    """Raised when package validation cannot produce reliable evidence."""


# Compatibility for callers importing the helper by its historical name.
PreflightError = PackageValidationError


def json_write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def dry_solve(conda_bin: str, environment_file: Path, platform: str) -> dict:
    """Return Conda's JSON plan without linking or downloading packages."""

    executable = shutil.which(conda_bin)
    if not executable:
        raise PackageValidationError(
            f"Conda solver not found: {conda_bin}. Install micromamba, mamba, or conda, "
            "then pass it with --conda-bin."
        )
    env = dict(os.environ)
    # CONDA_SUBDIR makes the solve reflect the requested target, not the host.
    env["CONDA_SUBDIR"] = platform
    command = [
        executable,
        "env",
        "create",
        "--file",
        str(environment_file),
        "--dry-run",
        "--json",
    ]
    completed = subprocess.run(command, text=True, capture_output=True, env=env)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PackageValidationError(
            f"Conda dry solve did not return JSON (exit {completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        ) from exc
    if completed.returncode != 0 or payload.get("success") is False:
        logs = payload.get("log_history") or []
        log_message = next(
            (
                str(item.get("message") or "").strip()
                for item in reversed(logs)
                if isinstance(item, dict) and str(item.get("message") or "").strip()
            ),
            "",
        )
        message = payload.get("message") or payload.get("error") or log_message or completed.stderr.strip()
        raise PackageValidationError(f"Conda dry solve failed: {message}")
    return payload


def resolved_packages(plan: dict) -> list[dict[str, str]]:
    """Normalize package records from Conda's dry-run FETCH/LINK actions."""

    actions = plan.get("actions") or {}
    records = actions.get("FETCH") or actions.get("LINK") or []
    if not isinstance(records, list) or not records:
        raise PackageValidationError("Conda dry solve returned no resolved package records.")

    packages: dict[tuple[str, str, str], dict[str, str]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        name = str(record.get("name") or "").strip()
        version = str(record.get("version") or "").strip()
        build = str(record.get("build") or record.get("build_string") or "").strip()
        if not name or not version:
            continue
        channel = str(record.get("channel") or "").strip()
        url = str(record.get("url") or "").strip()
        packages[(name.lower(), version, build)] = {
            "name": name,
            "version": version,
            "build": build,
            "channel": channel,
            "url": url,
        }
    if not packages:
        raise PackageValidationError("Conda dry solve records did not include package names and versions.")
    return sorted(packages.values(), key=lambda item: (item["name"].lower(), item["version"]))


def installed_packages(conda_bin: str, prefix: Path) -> list[dict[str, str]]:
    """Read the concrete inventory from a materialized Conda prefix."""

    if not prefix.is_dir():
        raise PackageValidationError(f"materialized environment prefix not found: {prefix}")
    executable = shutil.which(conda_bin)
    if not executable:
        raise PackageValidationError(f"Conda solver not found: {conda_bin}.")
    completed = subprocess.run(
        [executable, "list", "--prefix", str(prefix), "--json"],
        text=True,
        capture_output=True,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PackageValidationError(f"Could not read materialized Conda inventory: {completed.stderr.strip()}") from exc
    records = payload.get("packages") if isinstance(payload, dict) else payload
    if completed.returncode != 0 or not isinstance(records, list):
        raise PackageValidationError(f"Conda inventory failed: {completed.stderr.strip()}")
    packages = []
    for record in records:
        name = str(record.get("name") or "").strip()
        version = str(record.get("version") or "").strip()
        if name and version:
            packages.append({
                "name": name,
                "version": version,
                "build": str(record.get("build_string") or record.get("build") or "").strip(),
                "channel": str(record.get("channel") or "").strip(),
                "url": str(record.get("url") or "").strip(),
            })
    return sorted(packages, key=lambda item: (item["name"].lower(), item["version"]))


def inventory_difference(expected: list[dict[str, str]], installed: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """Compare the resolved plan and installed inventory by exact package identity."""

    def key(item: dict[str, str]) -> tuple[str, str, str]:
        return (item["name"].lower(), item["version"], item.get("build", ""))

    expected_by_key = {key(item): item for item in expected}
    installed_by_key = {key(item): item for item in installed}
    return {
        "missing_from_installed": [expected_by_key[item] for item in sorted(expected_by_key.keys() - installed_by_key.keys())],
        "unexpected_in_installed": [installed_by_key[item] for item in sorted(installed_by_key.keys() - expected_by_key.keys())],
    }


def conda_purl(package: dict[str, str]) -> str:
    """Build a stable PURL for a resolved Conda package."""

    name = quote(package["name"].lower(), safe=".-_")
    version = quote(package["version"], safe=".-_+")
    qualifiers = {
        "build": package.get("build", ""),
        "channel": package.get("channel", ""),
    }
    query = "&".join(
        f"{key}={quote(value, safe='.-_') }"
        for key, value in sorted(qualifiers.items())
        if value
    )
    return f"pkg:conda/{name}@{version}" + (f"?{query}" if query else "")


def explicit_conda_lock(packages: list[dict[str, str]]) -> str:
    """Render Conda's exact-artifact lockfile format from a dry solve."""

    urls = [package.get("url", "") for package in packages]
    if not all(urls):
        raise PackageValidationError("Cannot render explicit lockfile: a resolved package URL is missing.")
    return "@EXPLICIT\n" + "\n".join(urls) + "\n"


def resolved_environment_yaml(environment_file: Path, packages: list[dict[str, str]]) -> tuple[str, bool]:
    """Render an exact Conda environment only when the input opts in."""

    source = yaml.safe_load(environment_file.read_text(encoding="utf-8")) or {}
    if not bool(source.get("allow_resolved_versions", False)):
        return "", False
    resolved = {
        "name": str(source.get("name") or environment_file.stem),
        "channels": list(source.get("channels") or []),
        "dependencies": [
            f"{package['name']}={package['version']}={package['build']}"
            if package.get("build")
            else f"{package['name']}={package['version']}"
            for package in packages
        ],
        "allow_resolved_versions": True,
    }
    pip_dependencies = [item for item in source.get("dependencies") or [] if isinstance(item, dict) and "pip" in item]
    if pip_dependencies:
        resolved["dependencies"].append({"pip": list(pip_dependencies[0].get("pip") or [])})
    return yaml.safe_dump(resolved, sort_keys=False), True


def cyclonedx_sbom(packages: list[dict[str, str]], platform: str, environment_name: str) -> dict:
    """Represent the dry-resolved package plan as a CycloneDX 1.5 SBOM."""

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:conda-package-validation-{environment_name}-{platform}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": environment_name,
                "properties": [
                    {"name": "package-scanner:sbom-kind", "value": "dry-resolved-conda-plan"},
                    {"name": "package-scanner:platform", "value": platform},
                ],
            }
        },
        "components": [
            {
                "type": "library",
                "name": package["name"],
                "version": package["version"],
                "purl": conda_purl(package),
                "externalReferences": ([{"type": "distribution", "url": package["url"]}] if package.get("url") else []),
                "properties": [
                    {"name": "conda:build", "value": package.get("build", "")},
                    {"name": "conda:channel", "value": package.get("channel", "")},
                ],
            }
            for package in packages
        ],
    }


def trivy_assessment(trivy_bin: str, sbom_path: Path, out_path: Path) -> dict:
    """Scan the plan SBOM with Trivy without materializing the environment."""

    executable = shutil.which(trivy_bin)
    if not executable:
        raise PackageValidationError(
            f"Trivy scanner not found: {trivy_bin}. Install Trivy or use --skip-trivy only for an explicitly limited package validation."
        )
    completed = subprocess.run(
        [executable, "sbom", "--format", "json", "--output", str(out_path), str(sbom_path)],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise PackageValidationError(f"Trivy SBOM scan failed: {completed.stderr.strip() or completed.stdout.strip()}")
    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageValidationError(f"Trivy did not produce a valid report: {exc}") from exc
    findings: list[dict[str, str]] = []
    for result in data.get("Results") or []:
        if not isinstance(result, dict):
            continue
        for vulnerability in result.get("Vulnerabilities") or []:
            if not isinstance(vulnerability, dict):
                continue
            package_name = str(vulnerability.get("PkgName") or "").strip()
            vulnerability_id = str(vulnerability.get("VulnerabilityID") or "").strip()
            if not package_name or not vulnerability_id:
                continue
            findings.append(
                {
                    "package_name": package_name,
                    "package_version": str(vulnerability.get("InstalledVersion") or ""),
                    "vulnerability_id": vulnerability_id,
                    "severity": str(vulnerability.get("Severity") or "UNKNOWN").upper(),
                    "title": str(vulnerability.get("Title") or ""),
                    "reference_url": str(vulnerability.get("PrimaryURL") or ""),
                    "fixed_version": str(vulnerability.get("FixedVersion") or ""),
                }
            )
    return {"scanner": "trivy", "sbom": str(sbom_path), "findings": findings}


def post_json(url: str, payload: dict, timeout_seconds: int) -> dict:
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout_seconds, context=ssl.create_default_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url: str, timeout_seconds: int) -> dict:
    req = request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with request.urlopen(req, timeout=timeout_seconds, context=ssl.create_default_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def severity(vulnerability: dict) -> str:
    for source in (vulnerability.get("database_specific") or {}, vulnerability.get("ecosystem_specific") or {}):
        value = str(source.get("severity") or "").strip().upper()
        if value:
            return SEVERITY_ALIASES.get(value, value)
    return "UNKNOWN"


def fixed_versions(vulnerability: dict) -> list[str]:
    fixed: list[str] = []
    for affected in vulnerability.get("affected") or []:
        for range_entry in affected.get("ranges") or []:
            for event in range_entry.get("events") or []:
                value = str(event.get("fixed") or "").strip()
                if value and value not in fixed:
                    fixed.append(value)
    return fixed


def osv_assessment(packages: list[dict[str, str]], timeout_seconds: int, batch_size: int) -> dict:
    """Query OSV's PyPI ecosystem for the resolved names and versions.

    Conda does not publish an OSV ecosystem. Queries with no PyPI counterpart
    return no finding; the report explicitly preserves that coverage limit.
    """

    findings: list[dict[str, object]] = []
    queried: list[dict[str, str]] = []
    details_cache: dict[str, dict] = {}
    for start in range(0, len(packages), batch_size):
        batch = packages[start : start + batch_size]
        response = post_json(
            OSV_QUERY_BATCH_URL,
            {
                "queries": [
                    {"package": {"name": item["name"], "ecosystem": "PyPI"}, "version": item["version"]}
                    for item in batch
                ]
            },
            timeout_seconds,
        )
        results = response.get("results") or []
        if len(results) != len(batch):
            raise PackageValidationError("OSV querybatch response length did not match the package plan.")
        for package, result in zip(batch, results):
            queried.append(package)
            for summary in result.get("vulns") or []:
                vuln_id = str(summary.get("id") or "").strip()
                if not vuln_id:
                    continue
                if vuln_id not in details_cache:
                    details_cache[vuln_id] = get_json(f"{OSV_VULNERABILITY_URL}/{vuln_id}", timeout_seconds)
                full = details_cache[vuln_id]
                findings.append(
                    {
                        "package_name": package["name"],
                        "package_version": package["version"],
                        "vulnerability_id": vuln_id,
                        "severity": severity(full),
                        "summary": str(full.get("summary") or ""),
                        "reference_url": f"https://osv.dev/vulnerability/{vuln_id}",
                        "fixed_versions": fixed_versions(full),
                    }
                )
    findings.sort(key=lambda item: (str(item["severity"]), str(item["package_name"]), str(item["vulnerability_id"])))
    return {
        "scanner": "osv",
        "ecosystem": "PyPI",
        "coverage_note": "OSV has no Conda ecosystem. This checks resolved Conda package names and versions against PyPI advisories; native Conda package coverage requires a Conda-capable scanner.",
        "queried_packages": queried,
        "findings": findings,
    }


def gate(findings: list[dict[str, object]]) -> dict[str, object]:
    counts = Counter(str(item.get("severity") or "UNKNOWN").upper() for item in findings)
    blocking = [item for item in findings if str(item.get("severity") or "").upper() in BLOCKING_SEVERITIES]
    return {
        "policy": "High and Critical findings fail; Medium findings are reported but allowed.",
        "findings_by_severity": dict(sorted(counts.items())),
        "blocking_findings": len(blocking),
        "status": "fail" if blocking else "pass",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-file", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--conda-bin", default="conda")
    parser.add_argument("--platform", default="linux-64")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--skip-osv", action="store_true")
    parser.add_argument("--trivy-bin", default="trivy")
    parser.add_argument("--skip-trivy", action="store_true")
    parser.add_argument("--installed-prefix", type=Path, help="Compare and scan an already materialized Conda prefix.")
    args = parser.parse_args(argv)

    if not args.environment_file.is_file():
        raise SystemExit(f"environment file not found: {args.environment_file}")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be at least 1")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    try:
        plan = dry_solve(args.conda_bin, args.environment_file, args.platform)
        packages = resolved_packages(plan)
        json_write(args.out_dir / "conda-dry-run.json", plan)
        json_write(args.out_dir / "resolved-packages.json", {"platform": args.platform, "packages": packages})
        (args.out_dir / f"conda-{args.platform}.explicit.txt").write_text(explicit_conda_lock(packages), encoding="utf-8")
        resolved_yaml, resolved_yaml_allowed = resolved_environment_yaml(args.environment_file, packages)
        if resolved_yaml_allowed:
            (args.out_dir / "resolved-environment.yml").write_text(resolved_yaml, encoding="utf-8")
        environment_name = str(args.environment_file.stem)
        sbom_path = args.out_dir / "conda-resolved.cdx.json"
        json_write(sbom_path, cyclonedx_sbom(packages, args.platform, environment_name))
        report = {"scanner": "none", "findings": []} if args.skip_osv else osv_assessment(packages, args.timeout_seconds, args.batch_size)
        json_write(args.out_dir / "osv-report.json", report)
        trivy_report = {"scanner": "none", "findings": []} if args.skip_trivy else trivy_assessment(args.trivy_bin, sbom_path, args.out_dir / "trivy-sbom-report.json")
        if args.skip_trivy:
            json_write(args.out_dir / "trivy-sbom-report.json", trivy_report)
        installed_summary = None
        installed_trivy_report = {"scanner": "none", "findings": []}
        if args.installed_prefix:
            installed = installed_packages(args.conda_bin, args.installed_prefix)
            difference = inventory_difference(packages, installed)
            json_write(args.out_dir / "installed-packages.json", {"prefix": str(args.installed_prefix), "packages": installed})
            json_write(args.out_dir / "installed-inventory-difference.json", difference)
            installed_sbom_path = args.out_dir / "installed-conda.cdx.json"
            json_write(installed_sbom_path, cyclonedx_sbom(installed, args.platform, f"{environment_name}-installed"))
            installed_trivy_report = {"scanner": "none", "findings": []} if args.skip_trivy else trivy_assessment(args.trivy_bin, installed_sbom_path, args.out_dir / "installed-trivy-sbom-report.json")
            if args.skip_trivy:
                json_write(args.out_dir / "installed-trivy-sbom-report.json", installed_trivy_report)
            installed_summary = {
                "prefix": str(args.installed_prefix),
                "package_count": len(installed),
                "inventory_matches_dry_plan": not difference["missing_from_installed"] and not difference["unexpected_in_installed"],
                "difference": difference,
                "trivy": gate(list(installed_trivy_report.get("findings") or [])),
            }
        all_findings = list(report.get("findings") or []) + list(trivy_report.get("findings") or []) + list(installed_trivy_report.get("findings") or [])
        summary = {
            "environment_file": str(args.environment_file),
            "platform": args.platform,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "package_count": len(packages),
            "osv": gate(list(report.get("findings") or [])),
            "trivy": gate(list(trivy_report.get("findings") or [])),
            "vulnerability_gate": gate(all_findings),
            "installed_environment": installed_summary,
            "allow_resolved_versions": resolved_yaml_allowed,
            "artifacts": ["conda-dry-run.json", "resolved-packages.json", f"conda-{args.platform}.explicit.txt", "conda-resolved.cdx.json", "osv-report.json", "trivy-sbom-report.json"] + (["resolved-environment.yml"] if resolved_yaml_allowed else []),
        }
        json_write(args.out_dir / "package-validation-summary.json", summary)
        # Keep the old local filename for consumers of already deployed images.
        json_write(args.out_dir / "preflight-summary.json", summary)
    except PackageValidationError as exc:
        print(f"package validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["installed_environment"] and not summary["installed_environment"]["inventory_matches_dry_plan"]:
        return 3
    return 2 if summary["vulnerability_gate"]["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
