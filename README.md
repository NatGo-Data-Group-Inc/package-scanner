# package_scanner

Redeployable AWS infrastructure for package scanning, starting with Python/Conda and designed for a multi-platform build matrix.

Operator documentation:
- `docs/README.md`
- `docs/workflow.md`
- `docs/cli-switch-reference.md`
- `docs/troubleshooting.md`
- `docs/architecture.md`
- `docs/operations-runbook.md`
- `docs/security-and-governance.md`
- `docs/testing-and-quality.md`

Project policies:
- `CONTRIBUTING.md`
- `SECURITY.md`

## Scope (current)

- Python package scans from a Conda `environment.yml`.
- Platform-native scan runs:
  - linux/amd64
  - linux/arm64
  - windows/amd64
- Scanners:
  - `trivy` (SBOM scan)
  - `safety` (Python package vuln scan)
  - `fortify` (optional command hook)
- Governance artifacts generated per run:
  - approval candidate package list
  - vulnerability findings with CVE/NVD mapping
  - remediation-required list
  - remediation-exceptions list
  - remediation spreadsheet (action tracker)
- Split S3 model:
  - long-term evidence artifacts
  - short-lived deployment/build artifacts

## S3 Data Model

- Input contract:
  - `s3://<input-bucket>/inputs/python/environment.yml`
  - `s3://<input-bucket>/inputs/r/renv.lock`
- Evidence bucket (long-term):
  - `evidence/requirements/python/<platform>/<timestamp>/...`
  - `evidence/model-results/python/<platform>/<timestamp>/...`
  - `evidence/env-artifacts/python/<platform>/<timestamp>/...`
  - `evidence/governance/python/<platform>/<timestamp>/...`
  - `evidence/traceability/python/<platform>/<timestamp>/run-metadata.json`
  - reserved for future OCR/raw extraction: `evidence/raw-harvest/...`
- Ephemeral bucket (short-lived):
  - `deploy/tmp/python/<platform>/<timestamp>/...`
  - `deploy/tmp/r/<platform>/<timestamp>/...`
  - deleted by workflow after evidence write
  - lifecycle expiration is also enforced by bucket policy (3 days)

## Why this design

- Fully redeployable with CloudFormation.
- Native platform parity without managing static EC2 builders.
- Traceability is retained while temporary deploy artifacts are removed.
- Evidence paths cleanly separate requirements, model results, and environment packages.

## Project Layout

- `deployment/cfn/python-scan-stack.yaml`: AWS infrastructure and scan logic.
- `api/openapi.yaml`: control-plane API contract for UI/backend integration.
- `scripts/deploy-cfn.sh`: canonical deploy/update entrypoint.
- `scripts/start-python-scan.sh`: canonical scan start entrypoint.
- `scripts/start-r-scan.sh`: canonical R scan start entrypoint.
- `scripts/deploy-cfn.ps1`: PowerShell wrapper for `deploy-cfn.sh`.
- `scripts/start-python-scan.ps1`: PowerShell wrapper for `start-python-scan.sh`.
- `scripts/start-r-scan.ps1`: PowerShell wrapper for `start-r-scan.sh`.
- `scripts/r-lockfile-tools.sh`: helper to snapshot `renv.lock` files and bundle per-platform caches for offline/enclave delivery.
- `deployment/buildspecs/python-scan-buildspec.yml`: legacy buildspec (not used by matrix stack).
- `scripts/upload-buildspec.ps1`: legacy helper (not required by matrix stack).

## AWS Resources Created

- S3 input bucket (optional, created if not supplied).
- S3 evidence bucket (optional, created if not supplied).
- S3 ephemeral bucket (optional, created if not supplied).
- IAM Role for CodeBuild.
- 6 CodeBuild projects:
  - `${EnvironmentName}-python-scan-linux-amd64`
  - `${EnvironmentName}-python-scan-linux-arm64`
  - `${EnvironmentName}-python-scan-windows-amd64`
  - `${EnvironmentName}-r-scan-linux-amd64`
  - `${EnvironmentName}-r-scan-linux-arm64`
  - `${EnvironmentName}-r-scan-windows-amd64`

## Quick Start

1. Deploy the stack (canonical bash):

```bash
./scripts/deploy-cfn.sh \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --stack-name package-scanner-dev \
  --environment-name package-scanner-dev
```

This step also uploads `scripts/generate-governance-artifacts.py` to:
- `s3://<evidence-bucket>/config/generate-governance-artifacts.py`
and passes its SHA-256 into CloudFormation for runtime integrity verification.

2. Upload Conda environment file:

```powershell
aws s3 cp .\environment.yml s3://<input-bucket>/inputs/python/environment.yml --region us-east-1 --profile <aws-profile>
```

3. Start all platform scans (canonical bash):

```bash
./scripts/start-python-scan.sh \
  --stack-name package-scanner-dev \
  --input-bucket <input-bucket> \
  --input-object-key inputs/python/environment.yml \
  --safety-api-key <safety-api-key-placeholder> \
  --remediate-medium true \
  --fail-on-medium false \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token>
```

`EvidenceBucket` and `EphemeralBucket` are auto-resolved from stack outputs by default.
Only include `--safety-api-key` when supplying your licensed Safety token; omit the
flag to run without authenticated Safety.
Pass them only when overriding to alternate buckets.

4. Review evidence outputs:

- `s3://<evidence-bucket>/evidence/requirements/python/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/model-results/python/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/env-artifacts/python/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/governance/python/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/traceability/python/<platform>/<timestamp>/run-metadata.json`

R evidence outputs:
- `s3://<evidence-bucket>/evidence/requirements/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/model-results/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/env-artifacts/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/governance/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/traceability/r/<platform>/<timestamp>/...`

Governance outputs include:
- `approval-candidate-packages.csv`
- `vulnerability-findings.csv`
- `remediation-required.csv`
- `remediation-exceptions.csv`
- `remediation-spreadsheet.csv`
- `governance-summary.json`
- `governance-artifact-manifest.json`

Accuracy/safety behavior:
- scanner/report generation now fails closed if required artifacts are missing, empty, or invalid JSON.
- governance generation fails closed on schema mismatches.
- CodeBuild verifies governance script integrity (`SHA-256`) before execution.

Deployment guardrails:
- `deploy-cfn.sh`, `start-python-scan.sh`, and `start-r-scan.sh` require `--deployment-lock-token`.
- stack deploy writes CloudFormation tag `DeploymentLockToken=<token>` and all scan starts must match it.
- scripts validate caller identity with `aws sts get-caller-identity` and can enforce `--expected-account-id`.
- scripts require explicit `--profile` by default (`--allow-default-profile` can override this behavior).

PowerShell wrappers with the same behavior are still available for Windows usage:
- `.\scripts\deploy-cfn.ps1 ...`
- `.\scripts\start-python-scan.ps1 ...`
- `.\scripts\start-r-scan.ps1 ...`

## Optional Fortify Hook

Use scan-time overrides:

```bash
./scripts/start-python-scan.sh \
  --stack-name package-scanner-dev \
  --input-bucket <input-bucket> \
  --evidence-bucket <evidence-bucket> \
  --ephemeral-bucket <ephemeral-bucket> \
  --enable-fortify \
  --remediate-medium true \
  --fail-on-medium false \
  --fortify-command "sourceanalyzer ..."
```

## R Scan Start

1. Upload R lockfile:

```powershell
aws s3 cp .\renv.lock s3://<input-bucket>/inputs/r/renv.lock --region us-east-1 --profile <aws-profile>
```

2. Start R all-platform scans:

```bash
./scripts/start-r-scan.sh \
  --stack-name package-scanner-dev \
  --input-bucket <input-bucket> \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token>
```

## API Contract

Control-plane endpoints are defined in:
- `api/openapi.yaml`
- `api/examples/*.json` (request/response payload examples)

Primary endpoints:
- `POST /v1/scan-jobs`
- `GET /v1/scan-jobs/{job_id}`
- `GET /v1/scan-jobs/{job_id}/runs`
- `GET /v1/scan-jobs/{job_id}/artifacts`
- `POST /v1/exceptions`
- `GET /v1/exceptions/{exception_id}`
