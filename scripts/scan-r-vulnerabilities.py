#!/usr/bin/env python3
import argparse
import csv
import json
import ssl
import time
from pathlib import Path
from urllib import error, request


DEFAULT_OSV_API_URL = "https://api.osv.dev/v1/querybatch"
DEFAULT_OSV_VULN_BASE_URL = "https://api.osv.dev/v1/vulns"
DEFAULT_NVD_API_BASE_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
CRAN_ECOSYSTEM = "CRAN"
BIOCONDUCTOR_ECOSYSTEM = "Bioconductor"
SKIP_PRIORITIES = {"base", "recommended"}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_lockfile_metadata(path: Path):
    if not path.exists():
        return {}
    data = load_json(path)
    packages = data.get("Packages")
    if not isinstance(packages, dict):
        return {}
    out = {}
    for name, details in packages.items():
        if not isinstance(details, dict):
            continue
        out[str(name).strip()] = details
    return out


def infer_ecosystem(installed_row, lock_details):
    repository = str(installed_row.get("repository") or "").strip()
    source = str((lock_details or {}).get("Source") or "").strip()
    package_source = str((lock_details or {}).get("PackageSource") or "").strip()

    haystacks = [repository.lower(), source.lower(), package_source.lower()]
    if any("bioc" in item or "bioconductor" in item for item in haystacks):
        return BIOCONDUCTOR_ECOSYSTEM
    if any("cran" in item or "repository" == item for item in haystacks):
        return CRAN_ECOSYSTEM
    if repository.upper() == "CRAN":
        return CRAN_ECOSYSTEM
    return ""


def load_installed_packages(path: Path, lock_metadata):
    packages = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            name = str(raw.get("package_name") or raw.get("Package") or "").strip()
            version = str(raw.get("package_version") or raw.get("Version") or "").strip()
            priority = str(raw.get("priority") or raw.get("Priority") or "").strip().lower()
            if not name or not version:
                continue
            lock_details = lock_metadata.get(name, {})
            packages.append(
                {
                    "package_name": name,
                    "package_version": version,
                    "priority": priority,
                    "repository": str(raw.get("repository") or raw.get("Repository") or "").strip(),
                    "ecosystem": infer_ecosystem(raw, lock_details),
                }
            )
    return packages


def classify_severity(vuln):
    severity_entries = vuln.get("severity") or []
    for entry in severity_entries:
        if not isinstance(entry, dict):
            continue
        score = str(entry.get("score") or "").upper()
        if score.startswith("CVSS:3.") or score.startswith("CVSS:4."):
            parts = score.split("/")
            for part in parts:
                if part.startswith("AV:") or part.startswith("CVSS:"):
                    continue
            base = None
            for part in parts:
                if part.startswith("CVSS:"):
                    continue
                if part.startswith("S:"):
                    continue
                if part.startswith("BS:"):
                    try:
                        base = float(part.split(":", 1)[1])
                    except ValueError:
                        base = None
                    break
            if base is None and len(parts) >= 2:
                try:
                    base = float(parts[1])
                except ValueError:
                    base = None
            if base is not None:
                if base >= 9.0:
                    return "CRITICAL"
                if base >= 7.0:
                    return "HIGH"
                if base >= 4.0:
                    return "MEDIUM"
                if base > 0:
                    return "LOW"

    db_severity = (vuln.get("database_specific") or {}).get("severity")
    if isinstance(db_severity, str) and db_severity.strip():
        return db_severity.strip().upper()
    return "UNKNOWN"


def pick_reference_url(vuln):
    for ref in vuln.get("references") or []:
        if not isinstance(ref, dict):
            continue
        url = str(ref.get("url") or "").strip()
        if url:
            return url
    return f"https://osv.dev/vulnerability/{vuln.get('id', '')}".rstrip("/")


def collect_fixed_versions(vuln):
    fixed = []
    for affected in vuln.get("affected") or []:
        if not isinstance(affected, dict):
            continue
        for range_entry in affected.get("ranges") or []:
            if not isinstance(range_entry, dict):
                continue
            for event in range_entry.get("events") or []:
                if not isinstance(event, dict):
                    continue
                value = str(event.get("fixed") or "").strip()
                if value and value not in fixed:
                    fixed.append(value)
        versions = affected.get("versions") or []
        if isinstance(versions, list) and versions:
            last_version = str(versions[-1]).strip()
            if last_version and last_version not in fixed:
                fixed.append(last_version)
    return fixed


def post_json(url, payload, timeout_seconds):
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    context = ssl.create_default_context()
    with request.urlopen(req, timeout=timeout_seconds, context=context) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url, timeout_seconds):
    req = request.Request(url, headers={"Accept": "application/json"}, method="GET")
    context = ssl.create_default_context()
    with request.urlopen(req, timeout=timeout_seconds, context=context) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_nvd_severity(cve_id, api_base_url, timeout_seconds):
    data = get_json(f"{api_base_url}?cveId={cve_id}", timeout_seconds)
    vulnerabilities = data.get("vulnerabilities") or []
    if not vulnerabilities:
        return ""
    cve = (vulnerabilities[0] or {}).get("cve") or {}
    metrics = cve.get("metrics") or {}
    for key in ["cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"]:
        entries = metrics.get(key) or []
        for entry in entries:
            severity = str(entry.get("baseSeverity") or "").strip().upper()
            if severity:
                return severity
            cvss_data = entry.get("cvssData") or {}
            severity = str(cvss_data.get("baseSeverity") or "").strip().upper()
            if severity:
                return severity
    return ""


def fetch_osv_vulnerability(vuln_id, api_base_url, timeout_seconds):
    return get_json(f"{api_base_url.rstrip('/')}/{vuln_id}", timeout_seconds)


def query_osv(packages, api_url, osv_vuln_base_url, nvd_api_base_url, batch_size, timeout_seconds):
    findings = []
    queried = []
    skipped = []
    vuln_cache = {}

    queryable = []
    for pkg in packages:
        if pkg["priority"] in SKIP_PRIORITIES:
            skipped.append({**pkg, "skip_reason": f"priority={pkg['priority']}"})
            continue
        if not pkg["ecosystem"]:
            skipped.append({**pkg, "skip_reason": "unsupported-or-unknown-ecosystem"})
            continue
        queryable.append(pkg)

    for start in range(0, len(queryable), batch_size):
        batch = queryable[start : start + batch_size]
        payload = {
            "queries": [
                {
                    "package": {"name": pkg["package_name"], "ecosystem": pkg["ecosystem"]},
                    "version": pkg["package_version"],
                }
                for pkg in batch
            ]
        }
        response = post_json(api_url, payload, timeout_seconds)
        results = response.get("results") or []
        if len(results) != len(batch):
            raise RuntimeError("OSV querybatch response length did not match request length")
        for pkg, result in zip(batch, results):
            queried.append(pkg)
            vulns = result.get("vulns") or []
            for vuln in vulns:
                vuln_id = str(vuln.get("id") or "").strip()
                if not vuln_id:
                    continue
                if vuln_id not in vuln_cache:
                    try:
                        vuln_cache[vuln_id] = fetch_osv_vulnerability(vuln_id, osv_vuln_base_url, timeout_seconds)
                    except error.URLError:
                        vuln_cache[vuln_id] = vuln
                full_vuln = vuln_cache[vuln_id]
                fixed_versions = collect_fixed_versions(full_vuln)
                aliases = [alias for alias in (full_vuln.get("aliases") or []) if str(alias).strip()]
                findings.append(
                    {
                        "package_name": pkg["package_name"],
                        "package_version": pkg["package_version"],
                        "ecosystem": pkg["ecosystem"],
                        "vulnerability_id": vuln_id,
                        "aliases": aliases,
                        "severity": classify_severity(full_vuln),
                        "summary": str(full_vuln.get("summary") or "").strip(),
                        "details": str(full_vuln.get("details") or "").strip(),
                        "reference_url": pick_reference_url(full_vuln),
                        "fixed_versions": fixed_versions,
                        "fixed_available": bool(fixed_versions),
                    }
                )
        time.sleep(0.1)

    nvd_cache = {}
    for finding in findings:
        if finding["severity"] != "UNKNOWN":
            continue
        cve_aliases = [alias for alias in finding["aliases"] if alias.upper().startswith("CVE-")]
        for alias in cve_aliases:
            if alias not in nvd_cache:
                try:
                    nvd_cache[alias] = fetch_nvd_severity(alias, nvd_api_base_url, timeout_seconds)
                except error.URLError:
                    nvd_cache[alias] = ""
            if nvd_cache[alias]:
                finding["severity"] = nvd_cache[alias]
                break

    return {
        "scanner": "osv",
        "api_url": api_url,
        "osv_vuln_base_url": osv_vuln_base_url,
        "nvd_api_base_url": nvd_api_base_url,
        "queried_packages": queried,
        "skipped_packages": skipped,
        "findings": findings,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--installed-packages-file", required=True)
    ap.add_argument("--lock-file")
    ap.add_argument("--out-file", required=True)
    ap.add_argument("--osv-api-url", default=DEFAULT_OSV_API_URL)
    ap.add_argument("--osv-vuln-base-url", default=DEFAULT_OSV_VULN_BASE_URL)
    ap.add_argument("--nvd-api-base-url", default=DEFAULT_NVD_API_BASE_URL)
    ap.add_argument("--batch-size", type=int, default=200)
    ap.add_argument("--timeout-seconds", type=int, default=30)
    args = ap.parse_args(argv)

    installed_path = Path(args.installed_packages_file)
    if not installed_path.exists():
        raise SystemExit(f"installed-packages.csv not found: {installed_path}")

    lock_metadata = load_lockfile_metadata(Path(args.lock_file)) if args.lock_file else {}
    packages = load_installed_packages(installed_path, lock_metadata)
    report = query_osv(packages, args.osv_api_url, args.osv_vuln_base_url, args.nvd_api_base_url, args.batch_size, args.timeout_seconds)
    Path(args.out_file).write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
