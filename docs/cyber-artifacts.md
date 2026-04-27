## Cyber Handoff: Artifact Guide

Purpose: explain the bundle a Cyber analyst receives from the runs UI download button or S3 so they know what each file is and how to use it.

### Review Bundle Scope

The runs UI `download review bundle` action is the analyst-facing evidence bundle
for a single execution and platform.

Current Python review bundle contents:

- `materialization-summary.json`
- `governance-summary.json`
- `run-metadata.json`
- `approval-candidate-packages.csv`
- `vulnerability-findings.csv`
- `remediation-required.csv`
- `remediation-exceptions.csv`
- `remediation-spreadsheet.csv`
- `trivy-sbom-report.json`
- `safety-report.json`

Current R review bundle contents:

- `materialization-summary.json`
- `governance-summary.json`
- `run-metadata.json`
- `vulnerability-findings.csv`
- `remediation-required.csv`
- `remediation-exceptions.csv`
- `remediation-spreadsheet.csv`
- `trivy-sbom-report.json`
- `osv-report.json`

The review bundle is evidence for analyst disposition. It is not the enclave
deployable itself.

### Core Evidence Files
- `materialization-summary.json` — counts of restored packages, library paths, hash of the realized environment. Use to verify the environment actually materialized and to cross-check package counts.
- `run-metadata.json` — run IDs, timestamps, platform, stack info, task/bucket context, and artifact prefixes. Use to anchor any question back to the exact execution and artifact set.
- `governance-summary.json` — policy outcomes (`remediate_*`, `fail_on_*`), total findings, remediation counts, and approval-candidate counts. Use this as the one-file summary of what happened under policy.
- `installed-packages.csv` (R) / `requirements.lock.txt` (Python, outside the review bundle) — realized inventory after restore. Use to confirm the package/version was actually installed, not just declared in the source manifest.
- `preflight-native-deps.json` / `.txt` — native dependency preflight results. Shows whether required system headers/tools were present (gdal/geos/proj, mpfr, png/freetype, udunits2, cmake).

### Findings and Reports
- `approval-candidate-packages.csv` — full approved-for-review package inventory produced by the governance step. This is the package list Cyber should use to understand what is actually in scope for approval. For Python, it is the most useful inventory file to hand to Cyber from the review bundle.
- `vulnerability-findings.csv` — flattened findings with severity, package, version, advisory ID, aliases, fixed version, and remediation flags. This is the main per-finding review table.
- `remediation-required.csv` — subset of findings requiring action under current policy. If this file is non-empty, there is unresolved work under the policy in force for that run.
- `remediation-exceptions.csv` — findings currently categorized as exception. This does not mean "safe"; it means an exception path must be reviewed and explicitly accepted.
- `remediation-spreadsheet.csv` — same remediation data in a spreadsheet-friendly layout for review meetings and issue tracking.
- `trivy-sbom-report.json` (R/Python) — raw SBOM-based vulnerability output from Trivy. Use when Cyber wants scanner-native evidence, package path context, or to reconcile a CSV row back to the scanner output.
- `osv-report.json` (R) — OSV query results; contains queried packages and findings (including `UNKNOWN` severities). This is useful for R advisory enrichment and alias reconciliation.
- `safety-report.json` (Python, when present) — raw Safety output for pip dependencies. In the current workflow, this may be an empty array when Safety scanning is not actively keyed/enabled. Treat non-empty Safety results as supplemental Python evidence, not as a replacement for `vulnerability-findings.csv`.

### Offline Bundles
- `packages/offline/.../*.tar.gz` (+ `.sha256`) — offline cache/bundle for enclave import. This is the portable payload, not an evidence report.

### What `UNKNOWN` Severity Means
- A vulnerability was found but no reliable severity could be assigned by the scanner.
- Treat as “needs analyst triage,” not “safe” and not automatically “high.”
- Your handoff bundle includes OSV/NVD links to enrich the severity if CVE data exists.

### How to Use the Bundle Quickly
1. Verify run identity and scope:
   - open `run-metadata.json`
   - confirm execution id, platform, and timestamps
2. Verify what was actually built:
   - open `materialization-summary.json`
   - use `approval-candidate-packages.csv` as the Cyber-facing package inventory
3. Read the policy outcome:
   - open `governance-summary.json`
   - check `findings_total`, `required_total`, `exceptions_total`, and policy flags
4. Review findings:
   - start with `remediation-required.csv`
   - use `vulnerability-findings.csv` as the detailed analyst worksheet
   - use `trivy-sbom-report.json`, `safety-report.json`, or `osv-report.json` when you need scanner-native detail
5. Confirm whether a finding is actually in the environment:
   - use `approval-candidate-packages.csv`
   - if needed, cross-check against `requirements.lock.txt` or `installed-packages.csv` from the evidence prefixes
6. Keep deployable payload separate from evidence:
   - the review bundle is for analysis
   - the `.tar.gz` offline bundle artifacts are the enclave-transfer payloads

### Where This Bundle Comes From
- The runs UI “Download bundle” button zips these for the selected platform and execution.
- Paths are also in S3 under `evidence/...` for that run’s timestamp/platform.

### Quick Field Key
- `materialization-summary.json`: `counts.restored_packages`, `library_paths`
- `governance-summary.json`: `policy`, `counts.approval_candidates`, `counts.findings_total`, `counts.required_total`, `counts.exceptions_total`
- `osv-report.json`: `queried_packages`, `findings[*].severity|fixed_versions|aliases`
- `vulnerability-findings.csv`: `package_name`, `package_version`, `vulnerability_id`, `severity`, `fixed_available`
- `approval-candidate-packages.csv`: package inventory rows that represent what Cyber is being asked to review and approve

### Interpretation Notes

- `SUCCEEDED` execution does not mean `0` vulnerabilities. It means the scan workflow completed and artifact validation passed.
- Always distinguish:
  - workflow success/failure
  - policy failure/success
  - presence or absence of findings
- A run can succeed with findings when policy allows remediation tracking rather than hard fail.
- For Python specifically, a finding on `pip` or another dependency in `vulnerability-findings.csv` refers to the realized target environment unless proven otherwise. It is not automatically a defect in the scanner harness itself.
