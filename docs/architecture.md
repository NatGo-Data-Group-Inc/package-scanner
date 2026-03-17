# Architecture

## Overview

`package_scanner` uses a split control-plane/data-plane model.

- Control plane:
  - API contract for scan submission/status/artifacts (`api/openapi.yaml`)
- Data plane:
  - CloudFormation-managed CodeBuild matrix and S3 evidence model

## Data Flow

1. Operator deploys stack using `scripts/deploy-cfn.sh`.
2. Input artifact is uploaded:
   - Python: `inputs/python/environment.yml`
   - R: `inputs/r/renv.lock`
3. Operator starts scans:
   - Python: `scripts/start-python-scan.sh`
   - R: `scripts/start-r-scan.sh`
4. Python scan start resolves project names from stack outputs and triggers one `codebuild start-build` per platform.
5. R scan start triggers one Step Functions execution that:
   - plans staged restore batches from `renv.lock`
   - runs sequential CodeBuild stages per platform
   - validates final artifacts and writes an orchestration summary
6. Platform jobs:
   - fetch input
   - materialize the environment
   - generate SBOM/report data
   - generate governance CSV/JSON outputs
   - write evidence to S3
7. Ephemeral build artifacts are cleaned up from ephemeral prefix.

## AWS Resources

Template: `deployment/cfn/python-scan-stack.yaml`

- S3 Buckets:
  - input bucket
  - evidence bucket
  - ephemeral bucket
- IAM:
  - CodeBuild execution role
  - Step Functions execution role
  - Lambda role(s) for R planning/summary
- CodeBuild:
  - Python projects: 3
  - R projects: 3
- Step Functions:
  - R orchestrator: 1
- Lambda:
  - R stage planner
  - R scan summary/validation

## Tagging Strategy

All major resources are tagged for governance/ownership:

- `Project=package_scanner`
- `Purpose=<resource-specific-purpose>`
- `ManagedBy=cloudformation`
- `Environment=<EnvironmentName>`

Stack deployment also applies:

- `DeploymentLockToken=<token>`

## Guardrails

- Explicit profile enforcement (unless `--allow-default-profile`)
- STS account verification (`--expected-account-id`)
- Deployment lock token required for deploy and scan start
- Stack lock-token match check before start-build

## Evidence Model

Long-term evidence:

- `evidence/requirements/<ecosystem>/<platform>/<timestamp>/...`
- `evidence/model-results/<ecosystem>/<platform>/<timestamp>/...`
- `evidence/env-artifacts/<ecosystem>/<platform>/<timestamp>/...`
- `evidence/governance/<ecosystem>/<platform>/<timestamp>/...`
- `evidence/traceability/<ecosystem>/<platform>/<timestamp>/...`

Short-lived build artifacts:

- `deploy/tmp/python/<platform>/<timestamp>/...`
- `deploy/tmp/r/<platform>/<timestamp>/...`

R orchestration summaries:

- `evidence/orchestration/r/<execution-name>/orchestration-summary.json`

## Quality Controls

- fail-closed scanner/gov pipeline
- required non-empty artifact checks
- governance parsing/schema checks
- governance artifact hash manifest output
