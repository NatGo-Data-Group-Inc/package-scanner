#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import re
from pathlib import Path


class GovernanceError(RuntimeError):
    pass


def sha256_file(path: Path):
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


def csv_write(path: Path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def parse_requirements_lock(path: Path):
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


def severity_norm(value):
    if not value:
        return "UNKNOWN"
    return str(value).strip().upper()


def nvd_url(vuln_id):
    if vuln_id and str(vuln_id).upper().startswith("CVE-"):
        return f"https://nvd.nist.gov/vuln/detail/{vuln_id.upper()}"
    return ""


def parse_trivy(path: Path):
    out = []
    data = load_json(path)
    if not isinstance(data, dict):
        raise GovernanceError(f"Unexpected Trivy schema in {path}: root must be object")
    if "Results" not in data or not isinstance(data.get("Results"), list):
        raise GovernanceError(
            f"Unexpected Trivy schema in {path}: missing Results list"
        )

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


def parse_safety(path: Path):
    out = []
    data = load_json(path)
    if isinstance(data, dict):
        candidates = data.get("vulnerabilities")
        if isinstance(candidates, list):
            items = candidates
        else:
            if "issues" not in data:
                raise GovernanceError(
                    f"Unexpected Safety schema in {path}: expected vulnerabilities or issues list"
                )
            items = data.get("issues", [])
        if not isinstance(items, list):
            raise GovernanceError(
                f"Unexpected Safety schema in {path}: expected list for vulnerabilities/issues"
            )
    elif isinstance(data, list):
        items = data
    else:
        raise GovernanceError(f"Unexpected Safety schema in {path}")

    for item in items:
        if not isinstance(item, dict):
            raise GovernanceError(f"Unexpected Safety vulnerability entry in {path}")
        vuln_id = (
            item.get("vulnerability_id")
            or item.get("cve")
            or item.get("id")
            or ""
        )
        pkg = item.get("package_name", item.get("package", ""))
        if not pkg or not vuln_id:
            raise GovernanceError(
                f"Safety vulnerability missing package or vulnerability id in {path}"
            )
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


def dedupe_findings(rows):
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


def bool_arg(v, default=False):
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--platform", required=True)
    ap.add_argument("--remediate-medium", default="true")
    ap.add_argument("--fail-on-medium", default="false")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir)
    remediate_medium = bool_arg(args.remediate_medium, default=True)
    fail_on_medium = bool_arg(args.fail_on_medium, default=False)

    approval = parse_requirements_lock(run_dir / "requirements.lock.txt")
    for row in approval:
        row["platform"] = args.platform

    findings = dedupe_findings(
        parse_trivy(run_dir / "trivy-sbom-report.json")
        + parse_safety(run_dir / "safety-report.json")
    )
    for row in findings:
        row["platform"] = args.platform

    required_levels = {"CRITICAL", "HIGH"}
    if remediate_medium:
        required_levels.add("MEDIUM")

    remediation_required = []
    remediation_exceptions = []
    remediation_spreadsheet = []

    gate_levels = {"CRITICAL", "HIGH"}
    if fail_on_medium:
        gate_levels.add("MEDIUM")

    gate_hits = 0
    for f in findings:
        severity = f.get("severity", "UNKNOWN")
        fixed_available = f.get("fixed_available", "no")
        action_type = "upgrade" if fixed_available == "yes" else "exception"

        if severity in required_levels:
            req = {
                "platform": f.get("platform", ""),
                "package_name": f.get("package_name", ""),
                "current_version": f.get("package_version", ""),
                "severity": severity,
                "vulnerability_id": f.get("vulnerability_id", ""),
                "nvd_url": f.get("nvd_url", ""),
                "fixed_versions": f.get("fixed_versions", ""),
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
                "platform": f.get("platform", ""),
                "package_name": f.get("package_name", ""),
                "current_version": f.get("package_version", ""),
                "severity": severity,
                "vulnerability_id": f.get("vulnerability_id", ""),
                "nvd_url": f.get("nvd_url", ""),
                "recommended_version_or_replacement": f.get("fixed_versions", ""),
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
        run_dir / "approval-candidate-packages.csv",
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
        run_dir / "vulnerability-findings.csv",
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
        run_dir / "remediation-required.csv",
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
        run_dir / "remediation-exceptions.csv",
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
        run_dir / "remediation-spreadsheet.csv",
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
        p = run_dir / name
        output_manifest.append(
            {
                "path": str(p),
                "sha256": sha256_file(p),
                "size_bytes": p.stat().st_size,
            }
        )
    (run_dir / "governance-artifact-manifest.json").write_text(
        json.dumps(output_manifest, indent=2), encoding="utf-8"
    )

    summary = {
        "platform": args.platform,
        "artifacts": {
            "requirements_lock": {
                "path": str(run_dir / "requirements.lock.txt"),
                "sha256": sha256_file(run_dir / "requirements.lock.txt"),
            },
            "trivy_report": {
                "path": str(run_dir / "trivy-sbom-report.json"),
                "sha256": sha256_file(run_dir / "trivy-sbom-report.json"),
            },
            "safety_report": {
                "path": str(run_dir / "safety-report.json"),
                "sha256": sha256_file(run_dir / "safety-report.json"),
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
            "remediate_medium": remediate_medium,
            "fail_on_medium": fail_on_medium,
        },
    }
    (run_dir / "governance-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    if gate_hits > 0:
        raise SystemExit(3)


if __name__ == "__main__":
    try:
        main()
    except GovernanceError as exc:
        print(f"governance-error: {exc}")
        raise SystemExit(2)
