# Catalog And UI

The catalog layer turns raw S3 artifact prefixes into per-run records and stable
pointer documents.

## S3 Layout

- `evidence/catalog/r/runs/<execution-id>.json`
- `evidence/catalog/python/runs/<execution-id>.json`
- `evidence/catalog/r/pointers/latest-run.json`
- `evidence/catalog/python/pointers/latest-run.json`
- `evidence/catalog/r/pointers/latest-successful.json`
- `evidence/catalog/python/pointers/latest-successful.json`
- `evidence/catalog/r/pointers/current-approved.json`
- `evidence/catalog/python/pointers/current-approved.json`

## Catalog Utilities

Backfill:

```bash
python scripts/backfill-scan-catalog.py \
  --bucket <evidence-bucket> \
  --ecosystem r \
  --profile <aws-profile> \
  --write
```

List:

```bash
python scripts/list-scan-catalog.py \
  --bucket <evidence-bucket> \
  --ecosystem python \
  --profile <aws-profile>
```

Promote:

```bash
python scripts/promote-scan-run.py \
  --bucket <evidence-bucket> \
  --ecosystem r \
  --execution-id <execution-id> \
  --approved-by <operator> \
  --profile <aws-profile>
```

## Flask Browser

The Flask browser is a read-only view over the catalog:

```bash
./scripts/start-webapp.sh \
  --profile <aws-profile> \
  --catalog-bucket <evidence-bucket>
```

This wrapper starts the local webapp behind `gunicorn` and uses a dedicated
writable AWS home under `/tmp` so refreshed SSO credentials can be copied in
cleanly.

AWS-hosted deployment is now scaffolded separately from the scanner workers:

```bash
./scripts/deploy-webapp-cfn.sh \
  --profile <aws-profile>
```

Deployment notes:

- The hosted webapp runs behind an ALB and serves plain `HTTP` by default.
- Pass `--tls-certificate-arn <acm-certificate-arn>` to add an ALB `HTTPS`
  listener on `443`.
- When a certificate ARN is supplied, the ALB also redirects `HTTP:80` requests
  to `HTTPS:443`.
- The certificate must already exist in ACM in the same region as the ALB.
- The raw `*.elb.amazonaws.com` hostname cannot use an ACM-issued public
  certificate directly; use a DNS name you control and point it at the ALB.

For R ECS, the browser can watch more than one Step Functions orchestrator at
once. By default it includes the combined, Linux-only, and Windows-only R ECS
state machines. Override that set with `R_STATE_MACHINE_ARNS` as a comma-separated
list if an environment uses different names.

Stop it with:

```bash
./scripts/stop-webapp.sh
```

Routes:

- `/`
- `/runs/r`
- `/runs/python`
- `/runs/r/<execution-id>`
- `/runs/python/<execution-id>`
- `/runs/<ecosystem>/<execution-id>/<platform>/unknown-findings`
- `/cleanup/failed`

## Field Definitions

The runs page is intended to help operators choose a scan artifact set for
review, approval, or enclave transfer.

### Status

- `SUCCEEDED`: the selected platform execution completed successfully.
- `FAILED`: the selected platform execution failed.
- `RUNNING`: the selected platform execution is still in progress.
- `TIMED_OUT`: the selected platform execution exceeded its allowed runtime.
- `ABORTED`: the selected platform execution was stopped before normal completion.

On the runs page, `Status` is shown for the selected platform row, not as a
count across all platforms in the run.

Python dashboard note:
- A Python worker can write a late `completed` checkpoint before orchestration
  and catalog publication finish.
- The UI now treats that state as `Finalizing` while the Step Functions
  execution is still `RUNNING`.
- A globally green/completed Python run requires both:
  - Step Functions `SUCCEEDED`
  - the published catalog/orchestration record

This avoids the earlier failure mode where a retried run could appear fully
complete in the GUI even though the worker had died before final publication.

### Platform

`Platform` is the architecture view used for artifact selection:

- `linux-amd64`
- `linux-arm64` for Python ECS scans
- `windows`

The UI normalizes Windows platform labels to `windows` for selection purposes.
For example, a catalog record stored as `windows-amd64` is displayed and filtered
as `windows`.

### Validated

`Validated` means the platform produced a complete, expected artifact set in S3.
It is an artifact-completeness check, not a business approval.

A platform is marked `validated: true` only when:

- the platform status is `SUCCEEDED`
- the required traceability and requirements artifacts exist
- the required offline bundle artifacts exist

For R, this includes the expected package list, traceability summaries, and the
offline bundle tarball plus checksum. For Python, the same principle applies to
the Python traceability and evidence artifacts.

`Validated` does not mean:

- PMO approved the run
- vulnerabilities were accepted or resolved
- every platform in the broader run succeeded

### Severity `UNKNOWN`

When findings or remediation artifacts show severity `UNKNOWN`, interpret that
as:

- a finding exists
- the scanner could not determine a reliable severity
- Cyber analyst review is required to assign severity or disposition

`UNKNOWN` should not be interpreted as:

- no issue found
- safe to ignore
- automatically equivalent to `HIGH` or `CRITICAL`

For R workflows, `UNKNOWN` findings may still appear in remediation-required
outputs when the run is started with `--remediate-unknown true`.

The run detail page exposes two operator aids for `UNKNOWN` findings:

- an `UNKNOWN` review page that joins installed-package evidence, scanner
  outputs, external reference links, known fixed versions, and an analyst
  checklist
- an `UNKNOWN` analyst bundle zip containing the enriched finding export and
  source artifacts used to support severity/disposition review

### Package Count

`Package Count` is shown for the selected platform and is read from that
platform's `materialization-summary.json`.

- For R, it is `counts.restored_packages`.
- For Python, it is the sum of `counts.pip_package_count` and
  `counts.conda_package_count` when both are present.

This value is intended to support package-volume comparison during run
selection.

### Offline Bundle

For R, the offline bundle is the enclave-transfer artifact set under:

- `evidence/packages/offline/r/<platform>/<timestamp>/`

For R, the primary deployable set is the pair of offline archives together with
their `.sha256` checksum files:

- `renv-cache-<platform>-<timestamp>.tar.gz`
- `renv-library-<platform>-<timestamp>.tar.gz`

The GUI `download Posit handoff bundle` action packages those deployables
together with `renv.lock`, `installed-packages.csv`, `materialization-summary.json`,
`run-metadata.json`, and the local verifier scripts.

### For Cyber: Artifact Reference

See `docs/cyber-artifacts.md` for the authoritative handoff guide describing
every file in the review bundle, including:

- the full package inventory via `approval-candidate-packages.csv`
- Python `safety-report.json`
- Trivy/OSV raw scanner outputs
- governance and remediation CSVs
- materialization and run metadata context

### Default Filters

The runs page defaults are:

- `Status = SUCCEEDED`
- `Platform = linux-amd64`
- `Validated = yes`

These defaults are intended to surface the most immediately usable Linux scan
artifacts first.

## Failed Artifact Cleanup UI

The browser also exposes a `Cleanup Failed` page for operator-driven S3 cleanup.

Behavior:
- shows failed, timed out, or aborted cataloged runs
- supports preview before delete
- requires the exact execution id to be typed before deletion
- removes failed catalog records, orchestration prefixes, and checkpoint
  prefixes for the selected execution
- does not delete shared input objects from the scan input bucket
