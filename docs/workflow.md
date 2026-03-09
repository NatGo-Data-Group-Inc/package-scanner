# Workflow Guide

## 1) What This Project Does

`package_scanner` deploys AWS infrastructure that runs package scans and produces governance artifacts used for cyber approval decisions.

Core outputs per run include:

- `approval-candidate-packages.csv`
- `vulnerability-findings.csv`
- `remediation-required.csv`
- `remediation-exceptions.csv`
- `remediation-spreadsheet.csv`
- `governance-summary.json`
- `governance-artifact-manifest.json`

## 2) Architecture

### Control Plane

- API contract in `api/openapi.yaml`
- Supports `ecosystem` in scan requests:
  - `python`
  - `r`

### Data Plane

CloudFormation template: `deployment/cfn/python-scan-stack.yaml`

Resources:

- S3 input bucket
- S3 evidence bucket (long-term)
- S3 ephemeral bucket (short-lived)
- IAM role for CodeBuild
- CodeBuild projects:
  - Python:
    - `${EnvironmentName}-python-scan-linux-amd64`
    - `${EnvironmentName}-python-scan-linux-arm64`
    - `${EnvironmentName}-python-scan-windows-amd64`
  - R:
    - `${EnvironmentName}-r-scan-linux-amd64`
    - `${EnvironmentName}-r-scan-linux-arm64`
    - `${EnvironmentName}-r-scan-windows-amd64`

Guardrails:

- profile/account validation using STS
- required deployment lock token
- stack token match enforcement before starting scans

## 3) Prerequisites

- AWS CLI configured
- Access to target AWS account/region
- `bash` available (PowerShell wrappers call bash scripts)
- A lock token for your environment (for example: `dev-cyber-lock-v1`)

## 4) Deploy the Stack

```bash
./scripts/deploy-cfn.sh \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --stack-name package-scanner-dev \
  --environment-name package-scanner-dev
```

What this does:

- deploys/updates CloudFormation stack
- tags stack/resources for ownership and purpose
- uploads governance/SBOM generator scripts to evidence bucket config path

## 5) Python Scan: Example (`numpy`)

### Step A: prepare Python input

Create `environment.yml` with pinned packages:

```yaml
name: target
channels:
  - conda-forge
dependencies:
  - python=3.11
  - numpy=1.26.4
  - pip
```

### Step B: upload input

```bash
aws s3 cp ./environment.yml \
  s3://<input-bucket>/inputs/python/environment.yml \
  --region us-east-1 \
  --profile <aws-profile>
```

### Step C: start scan (all Python platforms)

```bash
./scripts/start-python-scan.sh \
  --stack-name package-scanner-dev \
  --input-bucket <input-bucket> \
  --input-object-key inputs/python/environment.yml \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --remediate-medium true \
  --fail-on-medium false
```

### Step D: get report artifacts

Evidence paths:

- `s3://<evidence-bucket>/evidence/governance/python/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/traceability/python/<platform>/<timestamp>/...`

## 6) R Scan: Example (`tidyverse`)

### Step A: prepare `renv.lock`

One way to generate it:

```r
install.packages("renv")
renv::init(bare = TRUE)
renv::install("tidyverse")
renv::snapshot()
```

This creates `renv.lock`.

### Step B: upload input

```bash
aws s3 cp ./renv.lock \
  s3://<input-bucket>/inputs/r/renv.lock \
  --region us-east-1 \
  --profile <aws-profile>
```

### Step C: start scan (all R platforms)

```bash
./scripts/start-r-scan.sh \
  --stack-name package-scanner-dev \
  --input-bucket <input-bucket> \
  --input-object-key inputs/r/renv.lock \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --remediate-medium true \
  --fail-on-medium false
```

### Step D: get report artifacts

Evidence paths:

- `s3://<evidence-bucket>/evidence/governance/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/traceability/r/<platform>/<timestamp>/...`

## 7) How to Scan Particular Package(s)

The scanner evaluates the resolved environment/lockfile. To scan specific packages:

- Python: pin target packages in `environment.yml` before upload.
  - Example: `numpy=1.26.4`, `pandas=2.2.3`
- R: include target packages in `renv.lock` by installing/snapshotting them first.
  - Example: `tidyverse`

## 8) Failure/Gate Behavior

- Runs are fail-closed for required scanner inputs/artifacts.
- Governance gate can fail run based on severity policy.
- `--fail-on-medium true` makes medium findings gate-fail.

## 9) PowerShell Users

Equivalent wrappers:

- `scripts/deploy-cfn.ps1`
- `scripts/start-python-scan.ps1`
- `scripts/start-r-scan.ps1`

Wrappers pass through to bash scripts with equivalent switches.

