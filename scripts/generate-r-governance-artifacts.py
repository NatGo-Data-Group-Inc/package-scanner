#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
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
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise GovernanceError(f"Invalid JSON in {path}: {exc}") from exc


def csv_write(path: Path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def parse_renv_lock(path: Path):
    data = load_json(path)
    packages = data.get("Packages")
    if not isinstance(packages, dict):
        raise GovernanceError(f"renv.lock missing Packages object: {path}")

    rows = []
    for name, details in packages.items():
        if not isinstance(details, dict):
            continue
        version = str(details.get("Version", "")).strip()
        if not version:
            continue
        rows.append(
            {
                "package_name": name,
                "package_version": version,
                "platform": "",
                "dependency_type": "unknown",
                "source": "renv.lock",
                "approval_status": "candidate",
            }
        )
    if not rows:
        raise GovernanceError(f"No packages parsed from {path}")
    return rows


def parse_installed_packages(path: Path):
    if not path.exists():
        raise GovernanceError(f"Required file missing: {path}")

    rows = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            name = str(raw.get("package_name") or raw.get("Package") or "").strip()
            version = str(raw.get("package_version") or raw.get("Version") or "").strip()
            if not name or not version:
                continue
            rows.append(
                {
                    "package_name": name,
                    "package_version": version,
                    "platform": "",
                    "dependency_type": "materialized",
                    "source": "installed-packages.csv",
                    "approval_status": "candidate",
                }
            )
    if not rows:
        raise GovernanceError(f"No installed packages parsed from {path}")
    return rows


def severity_norm(v):
    if not v:
        return "UNKNOWN"
    return str(v).strip().upper()


def nvd_url(vuln_id):
    if vuln_id and str(vuln_id).upper().startswith("CVE-"):
        return f"https://nvd.nist.gov/vuln/detail/{vuln_id.upper()}"
    return ""


def parse_trivy(path: Path):
    if not path.exists():
        return []
    data = load_json(path)
    if not isinstance(data, dict):
        return []
    if "Results" not in data or not isinstance(data.get("Results"), list):
        return []

    out = []
    for result in data.get("Results", []) or []:
        if not isinstance(result, dict):
            raise GovernanceError(f"Unexpected Trivy result entry in {path}")
        for vuln in result.get("Vulnerabilities", []) or []:
            if not isinstance(vuln, dict):
                raise GovernanceError(f"Unexpected Trivy vulnerability entry in {path}")
            vuln_id = vuln.get("VulnerabilityID", "")
            pkg = vuln.get("PkgName", "")
            if not pkg or not vuln_id:
                raise GovernanceError(f"Trivy vulnerability missing required fields in {path}")
            fixed = vuln.get("FixedVersion", "")
            out.append(
                {
                    "package_name": pkg,
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

    installed_packages_path = run_dir / "installed-packages.csv"
    if installed_packages_path.exists():
        approval = parse_installed_packages(installed_packages_path)
    else:
        approval = parse_renv_lock(run_dir / "renv.lock")
    for row in approval:
        row["platform"] = args.platform

    findings = parse_trivy(run_dir / "trivy-sbom-report.json")
    for row in findings:
        row["platform"] = args.platform

    required_levels = {"CRITICAL", "HIGH"}
    if remediate_medium:
        required_levels.add("MEDIUM")

    gate_levels = {"CRITICAL", "HIGH"}
    if fail_on_medium:
        gate_levels.add("MEDIUM")

    remediation_required = []
    remediation_exceptions = []
    remediation_spreadsheet = []
    gate_hits = 0

    for f in findings:
        sev = f.get("severity", "UNKNOWN")
        fixed_available = f.get("fixed_available", "no")
        action_type = "upgrade" if fixed_available == "yes" else "exception"

        if sev in required_levels:
            req = {
                "platform": f.get("platform", ""),
                "package_name": f.get("package_name", ""),
                "current_version": f.get("package_version", ""),
                "severity": sev,
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
                "severity": sev,
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
        if sev in gate_levels:
            gate_hits += 1

    csv_write(
        run_dir / "approval-candidate-packages.csv",
        ["platform", "package_name", "package_version", "dependency_type", "source", "approval_status"],
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
        ["platform", "package_name", "current_version", "severity", "vulnerability_id", "nvd_url", "fixed_versions", "fixed_available", "action_type"],
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

    manifest = []
    for name in [
        "approval-candidate-packages.csv",
        "vulnerability-findings.csv",
        "remediation-required.csv",
        "remediation-exceptions.csv",
        "remediation-spreadsheet.csv",
    ]:
        p = run_dir / name
        manifest.append({"path": str(p), "sha256": sha256_file(p), "size_bytes": p.stat().st_size})
    (run_dir / "governance-artifact-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    summary = {
        "platform": args.platform,
        "ecosystem": "r",
        "artifacts": {
            "renv_lock": {"path": str(run_dir / "renv.lock"), "sha256": sha256_file(run_dir / "renv.lock")},
            "trivy_report": {"path": str(run_dir / "trivy-sbom-report.json"), "sha256": sha256_file(run_dir / "trivy-sbom-report.json") if (run_dir / "trivy-sbom-report.json").exists() else "N/A"},
        },
        "counts": {
            "approval_candidates": len(approval),
            "findings_total": len(findings),
            "required_total": len(remediation_required),
            "exceptions_total": len(remediation_exceptions),
            "gate_hits": gate_hits,
        },
        "policy": {"remediate_medium": remediate_medium, "fail_on_medium": fail_on_medium},
    }
    if installed_packages_path.exists():
        summary["artifacts"]["installed_packages"] = {
            "path": str(installed_packages_path),
            "sha256": sha256_file(installed_packages_path),
        }
    materialization_summary_path = run_dir / "materialization-summary.json"
    if materialization_summary_path.exists():
        summary["artifacts"]["materialization_summary"] = {
            "path": str(materialization_summary_path),
            "sha256": sha256_file(materialization_summary_path),
        }
    (run_dir / "governance-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if gate_hits > 0:
        raise SystemExit(3)


if __name__ == "__main__":
    try:
        main()
    except GovernanceError as exc:
        print(f"governance-error: {exc}")
        raise SystemExit(2)
