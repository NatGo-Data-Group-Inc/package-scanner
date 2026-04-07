## Cyber Handoff: Artifact Guide

Purpose: explain the bundle a Cyber analyst receives from the runs UI download button or S3 so they know what each file is and how to use it.

### Core Evidence Files
- `materialization-summary.json` — counts of restored packages, library paths, hash of the realized environment. Use to verify the environment actually materialized and to cross-check package counts.
- `run-metadata.json` — run IDs, timestamps, platform, stack info, ephemeral prefixes. Use to anchor any questions back to the exact execution.
- `governance-summary.json` — policy outcomes (fail_on_* flags, remediation settings), finding counts, approval candidates. Use to see the policy context for the scan.
- `installed-packages.csv` (R) / `requirements.lock.txt` (Python) — realized inventory after restore. Use to confirm the package/version was actually installed, not just declared in the lockfile.
- `preflight-native-deps.json` / `.txt` — native dependency preflight results. Shows whether required system headers/tools were present (gdal/geos/proj, mpfr, png/freetype, udunits2, cmake).

### Findings and Reports
- `vulnerability-findings.csv` — flattened findings with severity, package, version, advisory ID, aliases, fixed version, and remediation flags.
- `remediation-required.csv` — subset of findings requiring action under current policy.
- `remediation-exceptions.csv` — findings marked as exception.
- `remediation-spreadsheet.csv` — same remediation data in a spreadsheet-friendly layout.
- `trivy-sbom-report.json` (R/Python) — SBOM-based vulnerability output from Trivy.
- `osv-report.json` (R) — OSV query results; contains queried packages and findings (including `UNKNOWN` severities).
- `safety-report.json` (Python, when present) — Safety DB findings for pip dependencies.

### Offline Bundles
- `packages/offline/.../*.tar.gz` (+ `.sha256`) — offline cache/bundle for enclave import. This is the portable payload, not an evidence report.

### What `UNKNOWN` Severity Means
- A vulnerability was found but no reliable severity could be assigned by the scanner.
- Treat as “needs analyst triage,” not “safe” and not automatically “high.”
- Your handoff bundle includes OSV/NVD links to enrich the severity if CVE data exists.

### How to Use the Bundle Quickly
1. Verify the run context: open `run-metadata.json` and `materialization-summary.json`.
2. Check preflight: `preflight-native-deps.*` should be `status: passed`; if failed, system deps were missing and findings may be incomplete.
3. Review findings:
   - Start with `remediation-required.csv` for the policy-driven to-do list.
   - Use `vulnerability-findings.csv` and `osv-report.json` for full detail and aliases.
   - For `UNKNOWN` severities, follow the links in `osv-report.json` or NVD aliases.
4. Confirm install reality: `installed-packages.csv` (R) or `requirements.lock.txt` (Python) to ensure the vulnerable package/version is actually present.
5. Offline payload: the `.tar.gz` in `packages/offline/...` is what goes into the enclave; not an evidence file.

### Where This Bundle Comes From
- The runs UI “Download bundle” button zips these for the selected platform and execution.
- Paths are also in S3 under `evidence/...` for that run’s timestamp/platform.

### Quick Field Key
- `materialization-summary.json`: `counts.restored_packages`, `library_paths`
- `governance-summary.json`: `policy`, `approval_candidates`, `findings_total`
- `osv-report.json`: `queried_packages`, `findings[*].severity|fixed_versions|aliases`
- `vulnerability-findings.csv`: `package_name`, `package_version`, `vulnerability_id`, `severity`, `fixed_available`
