#!/usr/bin/env python3
"""Standalone governance artifact generator for CodeBuild.

This script is uploaded to S3 and executed in CodeBuild where the
package_scanner package is not installed.  It therefore must be fully
self-contained.  The canonical implementation lives in
``package_scanner.governance``; this file is kept in sync manually.

When running locally with the repo on sys.path, importing from
``package_scanner.governance`` is preferred.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable, List, Sequence


class GovernanceError(RuntimeError):
    """Raised when required artifacts are missing or invalid."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path):
    if not path.exists():
        raise GovernanceError(f"Required file missing: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        raise GovernanceError(f"Invalid JSON in {path}: {exc}") from exc


def csv_write(path: Path, fieldnames: Sequence[str], rows: Iterable[dict]):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def parse_requirements_lock(path: Path) -> List[dict]:
    if not path.exists():
        raise GovernanceError(f"Required file missing: {path}")

    rows = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if not m:
            continue
        rows.append(
            {
                "package_name": m.group(1),
                "package_version": m.group(2),
                "platform": "",
                "dependency_type": "unknown",
                "source": "requirements.lock.txt",
                "approval_status": "candidate",
            }
        )
    if not rows:
        raise GovernanceError(
            f"No pinned requirements were parsed from {path}; expected package==version entries."
        )
    return rows


def parse_conda_list(path: Path) -> List[dict]:
    if not path.exists():
        return []
    data = load_json(path)
    if not isinstance(data, list):
        return []
    rows = []
    for item in data:
        if not isinstance(item, dict):
            raise GovernanceError(f"Unexpected conda-list entry in {path}")
        name = str(item.get("name") or "").strip()
        version = str(item.get("version") or "").strip()
        if not name or not version:
            continue
        rows.append(
            {
                "package_name": name,
                "package_version": version,
                "platform": "",
                "dependency_type": "unknown",
                "source": "conda-list.json",
                "approval_status": "candidate",
            }
        )
    return rows


def dedupe_approval(rows: Iterable[dict]) -> List[dict]:
    out: List[dict] = []
    seen = set()
    for row in rows:
        key = (
            str(row.get("package_name") or "").strip().lower(),
            str(row.get("package_version") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def severity_norm(value) -> str:
    if not value:
        return "UNKNOWN"
    return str(value).strip().upper()


def nvd_url(vuln_id: str) -> str:
    if vuln_id and str(vuln_id).upper().startswith("CVE-"):
        return f"https://nvd.nist.gov/vuln/detail/{vuln_id.upper()}"
    return ""


def parse_trivy(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out: List[dict] = []
    data = load_json(path)
    if not isinstance(data, dict):
        return []
    if "Results" not in data or not isinstance(data.get("Results"), list):
        return []

    for result in data.get("Results", []) or []:
        if not isinstance(result, dict):
            raise GovernanceError(f"Unexpected Trivy result entry in {path}")
        for vuln in result.get("Vulnerabilities", []) or []:
            if not isinstance(vuln, dict):
                raise GovernanceError(f"Unexpected Trivy vulnerability entry in {path}")
            if not vuln.get("PkgName") or not vuln.get("VulnerabilityID"):
                raise GovernanceError(
                    f"Trivy vulnerability missing required fields in {path}"
                )
            vuln_id = vuln.get("VulnerabilityID", "")
            fixed = vuln.get("FixedVersion", "")
            out.append(
                {
                    "package_name": vuln.get("PkgName", ""),
                    "package_version": vuln.get("InstalledVersion", ""),
                    "vulnerability_id": vuln_id,
                    "severity": severity_norm(vuln.get("Severity")),
                    "scanner": "trivy",
                    "title": vuln.get("Title", ""),
                    "reference_url": vuln.get("PrimaryURL", ""),
                    "nvd_url": nvd_url(vuln_id),
                    "fixed_versions": fixed,
                    "fixed_available": "yes" if str(fixed).strip() else "no",
                }
            )
    return out


def _parse_safety_v3(data: dict, path: Path) -> List[dict]:
    """Extract vulnerabilities from Safety CLI v3 schema (schema_version 3.0)."""
    out: List[dict] = []
    scan_results = data.get("scan_results", {})
    for project in scan_results.get("projects", []):
        for file_entry in project.get("files", []):
            results = file_entry.get("results", {})
            for dep in results.get("dependencies", []):
                pkg_name = dep.get("name", "")
                pkg_version = dep.get("version", "")
                for spec in dep.get("specifications", []):
                    for vuln in spec.get("vulnerabilities", []):
                        vuln_id = vuln.get("CVE", vuln.get("id", ""))
                        severity = vuln.get("severity", "")
                        fixed_list = vuln.get("fixed_versions", [])
                        if isinstance(fixed_list, list):
                            fixed = ";".join(str(x) for x in fixed_list if str(x).strip())
                        else:
                            fixed = str(fixed_list or "")
                        out.append(
                            {
                                "package_name": pkg_name,
                                "package_version": pkg_version,
                                "vulnerability_id": vuln_id,
                                "severity": severity_norm(severity),
                                "scanner": "safety",
                                "title": vuln.get("advisory", ""),
                                "reference_url": vuln.get("more_info_url", ""),
                                "nvd_url": nvd_url(vuln_id),
                                "fixed_versions": fixed,
                                "fixed_available": "yes" if fixed else "no",
                            }
                        )
    return out


def parse_safety(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out: List[dict] = []
    data = load_json(path)

    # Safety CLI v3 schema detection
    if isinstance(data, dict) and data.get("meta", {}).get("schema_version", "").startswith("3"):
        return _parse_safety_v3(data, path)

    if isinstance(data, dict):
        candidates = data.get("vulnerabilities")
        if isinstance(candidates, list):
            items = candidates
        elif "issues" in data:
            items = data.get("issues", [])
        else:
            raise GovernanceError(f"Unexpected Safety report schema in {path}")
        if not isinstance(items, list):
            raise GovernanceError(f"Unexpected Safety issues payload in {path}")
    elif isinstance(data, list):
        items = data
    else:
        raise GovernanceError(f"Unexpected Safety report payload in {path}")

    for item in items:
        if not isinstance(item, dict):
            continue
        vuln_id = (
            item.get("vulnerability_id")
            or item.get("cve")
            or item.get("id")
            or ""
        )
        pkg = item.get("package_name", item.get("package", ""))
        if not pkg or not vuln_id:
            continue
        fixed_list = item.get("fixed_versions", [])
        if isinstance(fixed_list, list):
            fixed = ";".join(str(x) for x in fixed_list if str(x).strip())
        else:
            fixed = str(fixed_list or "")
        out.append(
            {
                "package_name": pkg,
                "package_version": item.get(
                    "analyzed_version", item.get("installed_version", "")
                ),
                "vulnerability_id": vuln_id,
                "severity": severity_norm(item.get("severity")),
                "scanner": "safety",
                "title": item.get("advisory", item.get("summary", "")),
                "reference_url": item.get("more_info_url", item.get("url", "")),
                "nvd_url": nvd_url(vuln_id),
                "fixed_versions": fixed,
                "fixed_available": "yes" if fixed else "no",
            }
        )
    return out


def dedupe_findings(rows: Iterable[dict]) -> List[dict]:
    seen = set()
    out = []
    for row in rows:
        key = (
            row.get("package_name", ""),
            row.get("package_version", ""),
            row.get("vulnerability_id", ""),
            row.get("scanner", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def bool_arg(value, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def generate_governance_artifacts(
    run_dir: Path | str,
    platform: str,
    *,
    remediate_medium: bool = True,
    fail_on_medium: bool = False,
):
    """Generate governance artifacts in-place under *run_dir*."""

    run_path = Path(run_dir)
    remediate_medium_flag = bool(remediate_medium)
    fail_on_medium_flag = bool(fail_on_medium)

    requirements_path = run_path / "requirements.lock.txt"
    approval = dedupe_approval(
        (parse_requirements_lock(requirements_path) if requirements_path.exists() else [])
        + parse_conda_list(run_path / "conda-list.json")
    )
    if not approval:
        raise GovernanceError("No resolved package inventory was available for approval.")
    for row in approval:
        row["platform"] = platform

    findings = dedupe_findings(
        parse_trivy(run_path / "trivy-sbom-report.json")
        + parse_safety(run_path / "safety-report.json")
    )
    for row in findings:
        row["platform"] = platform

    required_levels = {"CRITICAL", "HIGH"}
    if remediate_medium_flag:
        required_levels.add("MEDIUM")

    remediation_required = []
    remediation_exceptions = []
    remediation_spreadsheet = []

    gate_levels = {"CRITICAL", "HIGH"}
    if fail_on_medium_flag:
        gate_levels.add("MEDIUM")

    gate_hits = 0
    for finding in findings:
        severity = finding.get("severity", "UNKNOWN")
        fixed_available = finding.get("fixed_available", "no")
        action_type = "upgrade" if fixed_available == "yes" else "exception"

        if severity in required_levels:
            req = {
                "platform": finding.get("platform", ""),
                "package_name": finding.get("package_name", ""),
                "current_version": finding.get("package_version", ""),
                "severity": severity,
                "vulnerability_id": finding.get("vulnerability_id", ""),
                "nvd_url": finding.get("nvd_url", ""),
                "fixed_versions": finding.get("fixed_versions", ""),
                "fixed_available": fixed_available,
                "action_type": action_type,
            }
            remediation_required.append(req)
            if fixed_available == "no":
                remediation_exceptions.append(
                    {
                        **req,
                        "exception_reason": "No fixed version available",
                        "compensating_controls": "",
                        "exception_expiration": "",
                        "approval_id": "",
                    }
                )

        remediation_spreadsheet.append(
            {
                "platform": finding.get("platform", ""),
                "package_name": finding.get("package_name", ""),
                "current_version": finding.get("package_version", ""),
                "severity": severity,
                "vulnerability_id": finding.get("vulnerability_id", ""),
                "nvd_url": finding.get("nvd_url", ""),
                "recommended_version_or_replacement": finding.get(
                    "fixed_versions", ""
                ),
                "fixed_available": fixed_available,
                "action_type": action_type,
                "owner": "",
                "target_date": "",
                "status": "open",
            }
        )

        if severity in gate_levels:
            gate_hits += 1

    csv_write(
        run_path / "approval-candidate-packages.csv",
        [
            "platform",
            "package_name",
            "package_version",
            "dependency_type",
            "source",
            "approval_status",
        ],
        approval,
    )
    csv_write(
        run_path / "vulnerability-findings.csv",
        [
            "platform",
            "package_name",
            "package_version",
            "vulnerability_id",
            "severity",
            "scanner",
            "title",
            "reference_url",
            "nvd_url",
            "fixed_versions",
            "fixed_available",
        ],
        findings,
    )
    csv_write(
        run_path / "remediation-required.csv",
        [
            "platform",
            "package_name",
            "current_version",
            "severity",
            "vulnerability_id",
            "nvd_url",
            "fixed_versions",
            "fixed_available",
            "action_type",
        ],
        remediation_required,
    )
    csv_write(
        run_path / "remediation-exceptions.csv",
        [
            "platform",
            "package_name",
            "current_version",
            "severity",
            "vulnerability_id",
            "nvd_url",
            "fixed_versions",
            "fixed_available",
            "action_type",
            "exception_reason",
            "compensating_controls",
            "exception_expiration",
            "approval_id",
        ],
        remediation_exceptions,
    )
    csv_write(
        run_path / "remediation-spreadsheet.csv",
        [
            "platform",
            "package_name",
            "current_version",
            "severity",
            "vulnerability_id",
            "nvd_url",
            "recommended_version_or_replacement",
            "fixed_available",
            "action_type",
            "owner",
            "target_date",
            "status",
        ],
        remediation_spreadsheet,
    )

    output_manifest = []
    for name in [
        "approval-candidate-packages.csv",
        "vulnerability-findings.csv",
        "remediation-required.csv",
        "remediation-exceptions.csv",
        "remediation-spreadsheet.csv",
    ]:
        p = run_path / name
        output_manifest.append(
            {
                "path": str(p),
                "sha256": sha256_file(p),
                "size_bytes": p.stat().st_size,
            }
        )
    (run_path / "governance-artifact-manifest.json").write_text(
        json.dumps(output_manifest, indent=2), encoding="utf-8"
    )

    summary = {
        "platform": platform,
        "artifacts": {
            "requirements_lock": {
                "path": str(run_path / "requirements.lock.txt"),
                "sha256": sha256_file(run_path / "requirements.lock.txt"),
            },
            "trivy_report": {
                "path": str(run_path / "trivy-sbom-report.json"),
                "sha256": sha256_file(run_path / "trivy-sbom-report.json") if (run_path / "trivy-sbom-report.json").exists() else "N/A",
            },
            "safety_report": {
                "path": str(run_path / "safety-report.json"),
                "sha256": sha256_file(run_path / "safety-report.json") if (run_path / "safety-report.json").exists() else "N/A",
            },
        },
        "counts": {
            "approval_candidates": len(approval),
            "findings_total": len(findings),
            "required_total": len(remediation_required),
            "exceptions_total": len(remediation_exceptions),
            "gate_hits": gate_hits,
            "findings_by_scanner": {
                "trivy": len([f for f in findings if f.get("scanner") == "trivy"]),
                "safety": len([f for f in findings if f.get("scanner") == "safety"]),
            },
        },
        "policy": {
            "remediate_medium": remediate_medium_flag,
            "fail_on_medium": fail_on_medium_flag,
        },
    }
    (run_path / "governance-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    return {
        "approval": approval,
        "findings": findings,
        "remediation_required": remediation_required,
        "remediation_exceptions": remediation_exceptions,
        "remediation_spreadsheet": remediation_spreadsheet,
        "manifest": output_manifest,
        "summary": summary,
        "gate_hits": gate_hits,
    }


def main(argv: Sequence[str] | None = None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--platform", required=True)
    ap.add_argument("--remediate-medium", default="true")
    ap.add_argument("--fail-on-medium", default="false")
    args = ap.parse_args(argv)

    result = generate_governance_artifacts(
        run_dir=args.run_dir,
        platform=args.platform,
        remediate_medium=bool_arg(args.remediate_medium, default=True),
        fail_on_medium=bool_arg(args.fail_on_medium, default=False),
    )

    if result["gate_hits"] > 0:
        raise SystemExit(3)

    return result


__all__ = [
    "GovernanceError",
    "bool_arg",
    "csv_write",
    "dedupe_findings",
    "generate_governance_artifacts",
    "load_json",
    "main",
    "nvd_url",
    "parse_requirements_lock",
    "parse_safety",
    "parse_trivy",
    "severity_norm",
    "sha256_file",
]

if __name__ == "__main__":
    try:
        main()
    except GovernanceError as exc:
        print(f"governance-error: {exc}")
        raise SystemExit(2)
