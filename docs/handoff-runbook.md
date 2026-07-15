# Handoff Runbook

This is the operator handoff guide for `package_scanner`.

Use this document when the system is being operated by someone who did not build it. It is written to be sufficient for normal deployment, scan execution, evidence review, and first-line troubleshooting without needing developer intervention.

## 1. Purpose

`package_scanner` runs package-security workflows for:

- Python environments defined by `environment.yml`
- R environments defined by `renv.lock`

It produces:

- platform-specific scan runs
  - Python: `linux-amd64`, `linux-arm64`, `windows-amd64`
  - R: `linux-amd64`, `windows-amd64`
- long-term evidence in S3
- governance outputs for cyber review
- for R, enclave-transferable offline cache bundles per platform

## 2. Operator Model

Normal operator responsibilities:

- deploy or update the stack
- submit Python and R scans
- monitor run status
- review evidence and governance outputs
- hand off offline bundles for enclave use
- rerun failed scans after correcting known operational issues

Tasks that should still be escalated:

- CloudFormation template changes
- IAM policy changes
- scanner logic changes
- unresolved AWS service defects

## 3. Prerequisites

- repository checkout at the approved commit
- AWS CLI configured
- access to the target AWS account and region
- an approved environment lock token
- `bash` available locally

Recommended local checks before operating:

```bash
aws sts get-caller-identity --profile <aws-profile>
./scripts/deploy-cfn.sh --help
./scripts/start-python-scan.sh --help
./scripts/start-r-scan.sh --help
```

## 4. Core Concepts

There are three buckets in the deployed system:

- input bucket
  - incoming `environment.yml` and `renv.lock`
- evidence bucket
  - long-term reports, summaries, manifests, and offline R bundles
- ephemeral bucket
  - short-lived intermediate build artifacts and R stage checkpoints

There are two scan styles:

- Python
  - one CodeBuild job per platform
- R
  - one Step Functions execution per scan
  - that execution runs long-lived ECS tasks per platform

There is also a separate operator webapp deployment:

- webapp
  - dedicated ECS Fargate service
  - ALB front end for the catalog and run browser
  - optional `HTTPS` when deployed with an ACM certificate ARN

## 5. Standard Deployment Procedure

Use the canonical deploy script:

```bash
./scripts/deploy-cfn.sh \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --stack-name cyber-scanner-dev \
  --environment-name package-scanner-dev \
  --s3-bucket <artifact-bucket-if-required>
```

What this does:

- deploys or updates the CloudFormation stack
- refreshes the uploaded runtime helper scripts in S3
- updates CodeBuild and Step Functions configuration

After deploy, verify outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name cyber-scanner-dev \
  --region us-east-1 \
  --profile <aws-profile> \
  --query "Stacks[0].Outputs"
```

## 5a. Webapp Deployment Procedure

Deploy the dedicated operator webapp separately from the scanner stacks:

```bash
./scripts/deploy-webapp-cfn.sh \
  --region us-east-1 \
  --profile <aws-profile> \
  --stack-name package-scanner-webapp-dev \
  --environment-name package-scanner-dev \
  --python-stack-name cyber-scanner-dev-python-ecs \
  --r-stack-name cyber-scanner-dev-r-ecs
```

Behavior:

- default deployment leaves the runtime disabled until explicitly enabled or a
  scan is detected
- use `./scripts/set-webapp-runtime.sh --action enable --profile <aws-profile>`
  for manual bring-up
- use `./scripts/set-webapp-runtime.sh --action disable --profile <aws-profile>`
  for manual shutdown
- while enabled, the runtime serves `HTTP` on ALB port `80`
- pass `--tls-certificate-arn <acm-certificate-arn>` to enable `HTTPS` on `443`
- when a certificate ARN is supplied, the ALB redirects `HTTP` to `HTTPS`
- the runtime controller keeps the GUI up while scan executions are running and
  shuts it down 60 minutes after the last execution completes
- because the ALB is deleted when idle, the runtime URL changes between start
  cycles; the `enable` command prints the current `WebappUrl` after each
  successful bring-up
- custom DNS remains available as a later environment-specific enhancement, but
  it is not the default dev workflow
- the certificate must already exist in ACM in the same region as the ALB

Verification:

```bash
WEBAPP_URL="$(aws cloudformation describe-stacks \
  --stack-name package-scanner-webapp-dev \
  --region us-east-1 \
  --profile <aws-profile> \
  --query "Stacks[0].Outputs[?OutputKey=='WebappUrl'].OutputValue | [0]" \
  --output text)"
curl -sS "${WEBAPP_URL}/healthz"
```

If browser access fails but command-line access works, suspect browser-side
HTTPS upgrade behavior before assuming the service is down.

## 6. Python Scan Procedure

### Prepare input

Upload the approved `environment.yml`:

```bash
aws s3 cp ./environment.yml \
  s3://<input-bucket>/inputs/python/environment.yml \
  --region us-east-1 \
  --profile <aws-profile>
```

### Start scan

```bash
./scripts/start-python-scan.sh \
  --stack-name cyber-scanner-dev \
  --input-bucket <input-bucket> \
  --input-object-key inputs/python/environment.yml \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --remediate-medium true \
  --fail-on-medium false \
  --safety-api-key <safety-token-if-used>
```

For ECS-backed launches, the start script now checks the target worker ASG
before submit. If the ASG is at `0`, it raises `MinSize` and
`DesiredCapacity` to at least `1` and waits for an active ECS container
instance.

### Monitor scan

The script returns three CodeBuild build IDs, one per platform.

Check build status:

```bash
aws codebuild batch-get-builds \
  --ids <build-id-1> <build-id-2> <build-id-3> \
  --region us-east-1 \
  --profile <aws-profile>
```

### Review outputs

Per platform:

- `evidence/requirements/python/<platform>/<timestamp>/`
- `evidence/model-results/python/<platform>/<timestamp>/`
- `evidence/env-artifacts/python/<platform>/<timestamp>/`
- `evidence/governance/python/<platform>/<timestamp>/`
- `evidence/traceability/python/<platform>/<timestamp>/`

Key files:

- `approval-candidate-packages.csv`
- `vulnerability-findings.csv`
- `remediation-required.csv`
- `remediation-exceptions.csv`
- `remediation-spreadsheet.csv`
- `governance-summary.json`
- `run-metadata.json`
- `safety-report.json`

For Cyber interpretation of the Python review bundle, use
`docs/cyber-artifacts.md` as the authoritative file-by-file reference.

## 7. R Scan Procedure

### Prepare input

Either upload the lockfile separately:

```bash
aws s3 cp ./renv.lock \
  s3://<input-bucket>/inputs/r/renv.lock \
  --region us-east-1 \
  --profile <aws-profile>
```

Or let the start script upload it:

```bash
--source-lock-file ./artifacts/renv.lock
```

### Start scan

```bash
./scripts/start-r-scan.sh \
  --stack-name cyber-scanner-dev \
  --input-bucket <input-bucket> \
  --input-object-key inputs/r/renv.lock \
  --source-lock-file ./artifacts/renv.lock \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --remediate-medium true \
  --fail-on-medium false \
  --remediate-unknown true \
  --fail-on-unknown false \
  --r-stage-package-count 10
```

For ECS-backed launches, the start script also performs a prescan ASG guard. If
the target worker ASG is scaled down to `0`, the launcher raises capacity to at
least `1` and waits for an active ECS container instance before starting the
execution.

Guidance on `--r-stage-package-count`:

- use smaller values when the lockfile is large or packages compile from source
- the current AWS account has an observed CodeBuild runtime ceiling of `45` minutes per build, so staged restores are required for large lockfiles
- `10` is a conservative operational value for large runs
- larger values are acceptable for small validation runs

### Monitor scan

R is event-driven. The workflow progresses without client polling once started.

The start script returns:

- Step Functions execution ARN
- execution name
- orchestration summary S3 path

Check status:

```bash
aws stepfunctions describe-execution \
  --execution-arn <execution-arn> \
  --region us-east-1 \
  --profile <aws-profile>
```

If you need stage-level visibility:

```bash
aws codebuild list-builds-for-project \
  --project-name package-scanner-dev-r-scan-linux-amd64 \
  --region us-east-1 \
  --profile <aws-profile> \
  --sort-order DESCENDING \
  --max-items 20
```

### Review outputs

Per platform:

- `evidence/requirements/r/<platform>/<timestamp>/`
- `evidence/model-results/r/<platform>/<timestamp>/`
- `evidence/env-artifacts/r/<platform>/<timestamp>/`
- `evidence/governance/r/<platform>/<timestamp>/`
- `evidence/traceability/r/<platform>/<timestamp>/`
- `evidence/packages/offline/r/<platform>/<timestamp>/`

R-specific key files:

- `installed-packages.csv`
- `materialization-summary.json`
- `session-info.txt`
- `restore.log`
- `osv-report.json`
- `renv-cache-<platform>-<timestamp>.tar.gz`
- `renv-cache-<platform>-<timestamp>.tar.gz.sha256`
- `renv-library-<platform>-<timestamp>.tar.gz`
- `renv-library-<platform>-<timestamp>.tar.gz.sha256`

## 8. Success Criteria

### Python

A Python run is considered complete when all three platform builds:

- show `SUCCEEDED`
- wrote governance outputs
- wrote `run-metadata.json`

### R

An R run is considered complete when:

- the Step Functions execution shows `SUCCEEDED`
- the orchestration summary reports all three platforms as `validated: true`
- each platform has:
  - `governance-summary.json`
  - `materialization-summary.json`
  - offline cache tarball + checksum
  - offline realized library tarball + checksum

## 9. S3 Navigation Cheatsheet

Find latest Python traceability:

```bash
aws s3 ls s3://<evidence-bucket>/evidence/traceability/python/linux-amd64/ --region us-east-1 --profile <aws-profile>
```

Find latest R orchestration summary:

```bash
aws s3 ls s3://<evidence-bucket>/evidence/orchestration/r/ --region us-east-1 --profile <aws-profile>
```

Open a specific summary:

```bash
aws s3 cp s3://<evidence-bucket>/evidence/traceability/r/windows-amd64/<timestamp>/governance-summary.json - \
  --region us-east-1 \
  --profile <aws-profile>
```

## 10. Enclave / Offline Transfer Procedure For R

After a successful R run:

1. retrieve:
   - `renv.lock`
   - `renv-cache-<platform>-<timestamp>.tar.gz`
   - `renv-cache-<platform>-<timestamp>.tar.gz.sha256`
   - `renv-library-<platform>-<timestamp>.tar.gz`
   - `renv-library-<platform>-<timestamp>.tar.gz.sha256`
2. verify checksum:

```bash
sha256sum -c renv-cache-<platform>-<timestamp>.tar.gz.sha256
sha256sum -c renv-library-<platform>-<timestamp>.tar.gz.sha256
```

3. transfer the files through the approved mechanism
4. in the enclave:
   - install matching R version
   - ensure `renv` is already installed on the base R image
   - unpack the cache archive into the cache root, not `/`
   - preserve the realized library archive and seed the project library from it before restore when validating a Posit/Workbench deployment
   - set `RENV_PATHS_CACHE`
   - run `renv::restore()` with public repos disabled so the validation is offline and uses the transferred artifacts only

Linux example:

```bash
mkdir -p /opt/renv/cache
tar -xzf renv-cache-<platform>-<timestamp>.tar.gz -C /opt/renv/cache
export RENV_PATHS_CACHE=/opt/renv/cache
export RENV_CONFIG_CACHE_SYMLINKS=FALSE
Rscript -e "options(repos=c(CRAN='file:///nonexistent-cran',RSPM='file:///nonexistent-rspm')); stopifnot(requireNamespace('renv', quietly=TRUE)); renv::consent(provided=TRUE); renv::restore(lockfile='renv.lock', prompt=FALSE, clean=TRUE)"
```

Validation:

- compare the restored package list to the approved `installed-packages.csv`
- compare package count to `materialization-summary.json`
- treat any attempted network download as a failed enclave restore test

## 11. First-Line Failure Handling

### Deployment failed

- inspect CloudFormation events
- correct parameter or permissions issues
- rerun `deploy-cfn.sh`

### Python scan failed

- collect build IDs
- inspect CodeBuild logs
- check input file path and evidence bucket outputs
- review `docs/troubleshooting.md`

### R scan failed

- inspect Step Functions execution
- identify failing platform/stage
- inspect the failing CodeBuild build record and CloudWatch logs
- review whether:
  - stage size was too large
  - platform package restore failed
  - external download failed
  - governance gate failed

### AWS CodeBuild timeout issue

The account currently exhibits an AWS-side behavior where builds are created with an effective `45` minute runtime even when configured higher.

Use the staged R workflow and, if needed, confirm with:

```bash
./scripts/diagnose-codebuild-timeouts.sh \
  --project-name package-scanner-dev-r-scan-linux-amd64 \
  --region us-east-1 \
  --profile <aws-profile> \
  --include-no-override \
  --override-timeout 60 \
  --override-timeout 120 \
  --override-timeout 480 \
  --output-file /tmp/codebuild-timeout-report.json
```

## 12. Change / Release Procedure

When turning over a new version:

1. record the git commit hash
2. deploy to the target environment
3. run one Python validation scan
4. run one R validation scan
5. verify S3 evidence paths
6. hand off:
   - stack name
   - region
   - AWS profile/account guidance
   - deployment lock token
   - current commit hash

## 13. Minimum Handoff Package

The following should always be supplied to the next operator:

- repository URL or checkout path
- approved commit hash
- target stack name
- AWS account and region
- environment lock token
- input bucket name
- evidence bucket name
- ephemeral bucket name
- this runbook
- `docs/troubleshooting.md`
- `docs/security-and-governance.md`
