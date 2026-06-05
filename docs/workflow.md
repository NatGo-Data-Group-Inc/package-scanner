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
- Step Functions:
  - `${EnvironmentName}-r-scan-orchestrator`

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

If you are starting from an already-materialized Conda environment and want a
candidate YAML for this repo rather than a raw export, capture it with:

```bash
python3 scripts/build-candidate-from-env-artifacts.py \
  --name <candidate-name> \
  --env-prefix <conda-env-prefix> \
  --conda-bin conda \
  --prefer-conda-available \
  --output candidates/<candidate-name>.yml \
  --artifacts-dir artifacts/<candidate-name> \
  --artifacts-zip artifacts/<candidate-name>.zip
```

That capture path records:
- `environment.yml`
- `conda list --json`
- `pip freeze`
- a normalized candidate YAML used by the Python ECS workflow

If you must change one package version in a candidate manually, prefer changing
the package version and leaving the build string off unless you have verified
that exact build exists in the configured channels. For example, prefer
`idna=3.15` over guessing a build string.

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
- `s3://<evidence-bucket>/evidence/model-results/python/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/requirements/python/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/packages/offline/python/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/env-artifacts/python/<platform>/<timestamp>/...`

Python offline environment note:

- `python-env-<platform>-<timestamp>.tar.gz` is a `conda-pack` archive of the realized target environment.
- Extract it at the destination prefix and run `conda-unpack` from inside that extracted environment.
- `python-pkgs-<platform>-<timestamp>.tar.gz` is the Micromamba/Conda package cache archive and can be used alongside the packed environment, but it is not itself the relocatable environment.
- GUI/operator expectation: the primary surfaced Python handoff download should be the direct `python-env-<platform>-<timestamp>.tar.gz` file plus its checksum, not the package-cache tarball.
- Completion expectation: treat a Python run as truly complete only when the
  Step Functions execution is `SUCCEEDED` and the catalog/orchestration records
  are published. A worker checkpoint can reach `completed` before the overall
  run is globally complete.

Python environment change policy:

- When a requester asks to add packages to an existing approved Python environment, the default workflow is to produce a new environment artifact, not to patch the enclave copy in place.
- The correct default sequence is:
  1. update the Python input artifact (`environment.yml`)
  2. resolve and materialize the changed environment in the build plane
  3. scan and govern the full realized environment
  4. deliver a new relocatable `conda-pack` archive
- This applies to both Linux and Windows Python environments.

Why this is the default:

- A single added package can force upgrades or downgrades of transitive dependencies.
- Conda and pip resolution outcomes are environment-wide decisions, not isolated package installs.
- Enclave-side wheel installation shifts compatibility risk to the least observable stage of the process.
- Cyber approval should cover the final realized environment that will actually be deployed, not only the originally approved base plus an informal add-on.

Overlay / wheel-only exception:

- A wheel-only or overlay-only delivery can be considered only when all of these are true:
  - the added package is pure Python
  - it does not force changes to already approved package versions
  - it does not introduce native-library, compiler, or ABI coupling
  - the target interpreter and dependency set are already known to be compatible
  - the overlay is tracked and governed as its own approved add-on
- If any of those conditions are not clearly true, rebuild and rescan the full environment instead.

Practical interpretation:

- For Conda-first environments, treat the changed request as a new environment candidate.
- For pip overlays on top of Conda, still prefer rebuilding the final full environment in the build plane.
- Do not use enclave-side wheel installation as the normal answer to “please add package X.”

## 6) R Scan: Example (`tidyverse`)

### Step A: prepare the R input

One way to generate it:

```r
install.packages("renv")
renv::init(bare = TRUE)
renv::install("tidyverse")
renv::snapshot()
```

This creates `renv.lock`.

Operator note:

- The scanner can now accept either:
  - an R `renv.lock`, or
  - a requested-package manifest (`requested-packages.json`) that lets the scanner resolve current package versions and then generate the realized `renv.lock` during materialization.
- For customer-driven package requests, the practical workflow is:
  - start from the requested package set,
  - generate or update the candidate `renv.lock`,
  - upload that candidate lockfile,
  - scan and approve that exact candidate.
- The approved candidate lockfile and the generated offline cache remain the deployment boundary for enclave delivery.

### Step B: upload input

```bash
aws s3 cp ./renv.lock \
  s3://<input-bucket>/inputs/r/renv.lock \
  --region us-east-1 \
  --profile <aws-profile>
```

You can skip this separate upload when the lockfile already exists locally in the repo and pass `--source-lock-file` in the next step instead.

To convert an existing lockfile into the new requested-package manifest format:

```bash
scripts/r-lockfile-tools.sh export-requested \
  --lockfile ./artifacts/renv.lock \
  --requested-out-file ./artifacts/requested-packages.json
```

To run the requested-package path later, use the same launcher with the new source switch:

```bash
scripts/start-r-scan.sh \
  --stack-name <stack> \
  --input-bucket <input-bucket> \
  --source-requested-file ./artifacts/requested-packages.json \
  --deployment-lock-token <token> \
  --profile <profile>
```

### Step C: start scan (all R platforms)

```bash
./scripts/start-r-scan.sh \
  --stack-name package-scanner-dev \
  --input-bucket <input-bucket> \
  --input-object-key inputs/r/renv.lock \
  --source-lock-file ./artifacts/renv.lock \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <12-digit-account-id> \
  --deployment-lock-token <env-lock-token> \
  --remediate-medium true \
  --fail-on-medium false
```

User-facing impact:

- Use the exact candidate lockfile path you intend to approve and deploy.
- For named candidates, prefer `inputs/r/candidates/<candidate-id>/<platform>/<timestamp>/renv.lock` instead of overwriting only `inputs/r/renv.lock`.
- For requested-package candidates, prefer `inputs/r/candidates/<candidate-id>/<platform>/<timestamp>/requested-packages.json`.
- When the requested-package path is used, the run still emits `evidence/requirements/r/<platform>/<timestamp>/renv.lock`; that file is the generated, realized lockfile that cyber signs off on.
- The ECS orchestrators now resolve the latest active task-definition family revision automatically, so newly registered scanner images are picked up by future runs without editing the Step Functions definition again.

### Step D: get report artifacts

Evidence paths:

- `s3://<evidence-bucket>/evidence/orchestration/r/<execution-name>/orchestration-summary.json`
- `s3://<evidence-bucket>/evidence/governance/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/traceability/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/packages/offline/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/model-results/r/<platform>/<timestamp>/...`
- `s3://<evidence-bucket>/evidence/requirements/r/<platform>/<timestamp>/...`

## 7) Offline Bundle & Enclave Workflow

When an enclave cannot reach the internet, treat the `renv.lock` as your blueprint and move only portable artifacts that have already passed cyber review.

1. **Snapshot the approved environment** (on a connected build host):
   ```bash
   ./scripts/r-lockfile-tools.sh snapshot \
     --project-dir /path/to/project \
     --lockfile renv.lock
   ```
   Commit the lockfile so every change is auditable.
2. **Run `scripts/start-r-scan.sh`** against the lockfile. It starts the R orchestration state machine, which first plans staged restore batches from `renv.lock`, then fans out to the three platform jobs. Each platform processes those stages sequentially, checkpointing the cache and library between CodeBuild runs before the final stage emits evidence artifacts and the enclave-transferable cache bundle.
3. **Collect the generated deployable bundle** from `s3://<evidence>/evidence/packages/offline/r/<platform>/<timestamp>/`. Each run writes both a cache archive and a realized library archive (for example `renv-cache-linux-amd64-<ts>.tar.gz` and `renv-library-linux-amd64-<ts>.tar.gz`) together with matching `.sha256` files.
4. **Review the materialization evidence** in `evidence/env-artifacts/r/<platform>/<timestamp>/`, including `installed-packages.csv`, `session-info.txt`, `restore.log`, and `materialization-summary.json`.
5. **Stage artifacts for the enclave** by transferring the lockfile, cache archive, realized library archive, and checksums through the approved path. Inside the enclave, set `RENV_PATHS_CACHE` to the unpacked cache, seed the project library from the realized library archive, install R 4.4.0, and run `renv::restore()` offline.

## 8) How to Scan Particular Package(s)

The scanner evaluates the resolved environment/lockfile. To scan specific packages:

- Python: pin target packages in `environment.yml` before upload.
  - Example: `numpy=1.26.4`, `pandas=2.2.3`
- R: include target packages in `renv.lock` by installing/snapshotting them first.
  - Example: `tidyverse`

## 9) Failure/Gate Behavior

- Runs are fail-closed for required scanner inputs/artifacts.
- Governance gate can fail run based on severity policy.
- `--fail-on-medium true` makes medium findings gate-fail.
- R also supports:
  - `--remediate-unknown true|false`
  - `--fail-on-unknown true|false`
- Large R lockfiles should use staged restores via `--r-stage-package-count`.

### R Restore Diagnostics

- Failed R ECS runs now emit `restore-root-cause.txt` in `evidence/traceability/r/<platform>/<timestamp>/`.
- The dashboard latest-failed section shows this concise root cause when available.
- The restore path now:
  - validates only `Source: Repository` packages against repo visibility,
  - does not falsely reject GitHub-sourced packages such as `ROhdsiWebApi`,
  - retries still-missing packages once after the initial `renv::restore()` pass to handle dependency-ordering gaps.

### Interpreting `UNKNOWN` Severity

- `UNKNOWN` means the scanner identified a finding but could not assign a
  reliable severity from the available vulnerability metadata.
- Treat `UNKNOWN` as unresolved severity, not as cleared and not automatically
  as high severity.
- For Cyber handoff, interpret `UNKNOWN` as "analyst review required to assign
  severity and disposition."
- When `--remediate-unknown true` is used, `UNKNOWN` findings are included in
  remediation-required outputs.
- When `--fail-on-unknown true` is used, `UNKNOWN` findings also gate-fail the
  run.

## 10) PowerShell Users

Equivalent wrappers:

- `scripts/deploy-cfn.ps1`
- `scripts/start-python-scan.ps1`
- `scripts/start-r-scan.ps1`

Wrappers pass through to bash scripts with equivalent switches.

## 11) Turnover Guidance

For operator handoff and steady-state execution, use:

- [handoff-runbook.md](./handoff-runbook.md)
- [operations-runbook.md](./operations-runbook.md)
- [troubleshooting.md](./troubleshooting.md)
