# package_scanner

Redeployable AWS infrastructure for package scanning across Python and R with
platform-specific execution paths:
- Python currently runs on CodeBuild for all-platform scans and can run on ECS
  on EC2 for Linux-only scans.
- R is migrating to ECS on EC2 for long-running materialization and scan work.

Operator documentation:
- `docs/handoff-runbook.md`
- `docs/README.md`
- `docs/catalog-and-ui.md`
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
- R package scans from an `renv.lock`.
- Python platform-native scan runs:
  - linux/amd64
  - linux/arm64
  - windows/amd64
- R platform-native scan runs:
  - linux/amd64
  - windows/amd64
- Scanners:
  - `trivy` (SBOM scan)
  - `safety` (Python package vuln scan)
  - `osv.dev` (R package vuln scan via CRAN/Bioconductor ecosystem matching)
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
- Python stays on a simple CodeBuild path.
- R uses ECS on EC2 because package materialization can exceed practical CodeBuild limits.
- Traceability is retained while temporary deploy artifacts are removed.
- Evidence paths cleanly separate requirements, model results, and environment packages.

## Project Layout

- `deployment/cfn/python-scan-stack.yaml`: Python scan infrastructure and the preserved CodeBuild-based path.
- `deployment/cfn/python-ecs-scan-stack.yaml`: Python ECS infrastructure for Linux amd64/arm64 scanning.
- `deployment/cfn/ecs-scan-stack.yaml`: R ECS infrastructure and orchestration.
- `webapp/app.py`: read-only Flask browser for the scan catalog.
- `api/openapi.yaml`: control-plane API contract for UI/backend integration.
- `scripts/deploy-cfn.sh`: canonical deploy/update entrypoint.
- `scripts/deploy-python-ecs-cfn.sh`: deploy/update entrypoint for the Python ECS stack.
- `scripts/deploy-r-ecs-cfn.sh`: deploy/update entrypoint for the R ECS stack.
- `scripts/start-python-scan.sh`: canonical scan start entrypoint.
- `scripts/build-python-ecs-images.sh`: build/push entrypoint for the Python ECS Linux image.
- `scripts/start-r-scan.sh`: canonical R scan start entrypoint. Starts the Step Functions orchestration for the ECS R workflow.
- `scripts/deploy-cfn.ps1`: PowerShell wrapper for `deploy-cfn.sh`.
- `scripts/start-python-scan.ps1`: PowerShell wrapper for `start-python-scan.sh`.
- `scripts/start-r-scan.ps1`: PowerShell wrapper for `start-r-scan.sh`.
- `scripts/register-r-task-def.sh`: register a new R linux task definition revision with an immutable image tag.
- `scripts/r-lockfile-tools.sh`: helper to snapshot `renv.lock` files and bundle per-platform caches for offline/enclave delivery.
- `deployment/buildspecs/python-scan-buildspec.yml`: legacy buildspec (not used by matrix stack).
- `scripts/upload-buildspec.ps1`: legacy helper (not required by matrix stack).

## AWS Resources Created

- Shared S3 input/evidence/ephemeral buckets (optional, created if not supplied).
- Python stack:
  - CodeBuild projects for Python platform scans
- Python ECS stack:
  - ECS clusters for Linux Python workers
  - ECR repository for Linux Python image
  - Step Functions state machine for Linux-only Python ECS scans
- R ECS stack:
  - ECS clusters for Linux and Windows R workers
  - ECR repositories for Linux and Windows R images
  - Step Functions state machines for:
    - all-platform R ECS scans
    - Linux-only R ECS scans

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

3. Start all-platform Python scans on CodeBuild (canonical bash):

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

Linux-only Python ECS path:

4. Deploy the Python ECS stack:

```bash
./scripts/deploy-python-ecs-cfn.sh \
  --stack-name cyber-scanner-dev-python-ecs \
  --environment-name package-scanner-dev \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --existing-input-bucket-name <input-bucket> \
  --existing-evidence-bucket-name <evidence-bucket> \
  --existing-ephemeral-bucket-name <ephemeral-bucket> \
  --s3-bucket <evidence-bucket>
```

5. Build and push the Python ECS Linux image:

```bash
./scripts/build-python-ecs-images.sh \
  --stack-name cyber-scanner-dev-python-ecs \
  --region us-east-1 \
  --profile <aws-profile>
```

6. Start a Linux-only Python ECS scan:

```bash
./scripts/start-python-scan.sh \
  --stack-name cyber-scanner-dev-python-ecs \
  --input-bucket <input-bucket> \
  --source-environment-file ./environment.yml \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --platform-set linux-only
```

7. Review Python evidence outputs:

- `s3://<evidence-bucket>/evidence/requirements/python/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/model-results/python/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/env-artifacts/python/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/governance/python/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/traceability/python/<platform>/<timestamp>/run-metadata.json`
- `s3://<evidence-bucket>/evidence/catalog/python/...`
- `s3://<evidence-bucket>/evidence/catalog/r/...`

5. Deploy the R ECS stack:

```bash
./scripts/deploy-r-ecs-cfn.sh \
  --stack-name cyber-scanner-dev-r-ecs \
  --environment-name package-scanner-dev \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --existing-input-bucket-name <input-bucket> \
  --existing-evidence-bucket-name <evidence-bucket> \
  --existing-ephemeral-bucket-name <ephemeral-bucket> \
  --s3-bucket <evidence-bucket>
```

6. Build and push the R ECS images:

```bash
./scripts/build-r-ecs-images.sh \
  --stack-name cyber-scanner-dev-r-ecs \
  --region us-east-1 \
  --profile <aws-profile>
```

The Windows R image must be built from a Windows Docker host. See
`docs/ecs-cutover-runbook.md`.

7. Start R scans:

```bash
./scripts/start-r-scan.sh \
  --stack-name cyber-scanner-dev-r-ecs \
  --input-bucket <input-bucket> \
  --source-lock-file ./artifacts/renv.lock \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --remediate-medium true \
  --fail-on-medium false \
  --remediate-unknown true \
  --fail-on-unknown false \
  --platform-set all
```

Use `--platform-set linux-only` to validate Linux without waiting on the Windows image:

```bash
./scripts/start-r-scan.sh \
  --stack-name cyber-scanner-dev-r-ecs \
  --input-bucket <input-bucket> \
  --source-lock-file ./artifacts/renv.test.lock \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --platform-set linux-only
```

8. Review R evidence outputs:
- `s3://<evidence-bucket>/evidence/requirements/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/model-results/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/env-artifacts/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/governance/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/traceability/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/packages/offline/r/<platform>/<timestamp>/...`

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

Python environment artifact behavior:
- the Python target environment bundle is produced with `conda-pack`
- after extraction at the destination prefix, run `conda-unpack`
- this replaces the older plain tar/extract behavior and is intended for relocatable Python environment delivery

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

## R Execution Model

The R ECS workflow materializes the environment on each platform before report generation:
- install the requested R runtime from `renv.lock`
- restore the environment inside long-lived ECS tasks
- emit `installed-packages.csv`, `session-info.txt`, and restore logs
- publish a platform cache bundle and checksum for enclave transfer

For daily operation and handoff, use:

- `docs/handoff-runbook.md`
- `docs/operations-runbook.md`
- `docs/troubleshooting.md`

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
