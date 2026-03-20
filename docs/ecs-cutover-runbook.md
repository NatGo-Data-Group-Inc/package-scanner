# ECS R Cutover Runbook

This document is the operator procedure for cutting the R scan worker plane over
from CodeBuild to ECS on EC2.

## Scope

- Replace the R worker plane only.
- Preserve:
  - existing S3 buckets
  - evidence layout
  - governance outputs
  - Step Functions entrypoint contract
- Python remains unchanged for now.

## Target State

- One ECS Linux cluster with:
  - one `linux-amd64` EC2 worker
- One ECS Windows cluster with:
  - one `windows-amd64` EC2 worker
- One long-running ECS task per platform per R scan.
- Checkpointing remains enabled for status and recovery only.

## Preconditions

- AWS profile has deployment permissions.
- Docker with `buildx` is available on a Linux machine for Linux image build.
- A Windows Docker builder is available for the Windows image build.
- The updated repo contents are present locally.

## Key Files

- ECS stack template:
  - [ecs-scan-stack.yaml](/home/tjansto/github/natgo/package-scanner/deployment/cfn/ecs-scan-stack.yaml)
- ECS deploy entrypoint:
  - [deploy-r-ecs-cfn.sh](/home/tjansto/github/natgo/package-scanner/scripts/deploy-r-ecs-cfn.sh)
- Linux image:
  - [r-linux.Dockerfile](/home/tjansto/github/natgo/package-scanner/docker/r-linux.Dockerfile)
- Windows image:
  - [r-windows.Dockerfile](/home/tjansto/github/natgo/package-scanner/docker/r-windows.Dockerfile)
- Linux task entrypoint:
  - [run-r-ecs-task.sh](/home/tjansto/github/natgo/package-scanner/scripts/run-r-ecs-task.sh)
- Windows task entrypoint:
  - [run-r-ecs-task.ps1](/home/tjansto/github/natgo/package-scanner/scripts/run-r-ecs-task.ps1)
- Linux image build helper:
  - [build-r-ecs-images.sh](/home/tjansto/github/natgo/package-scanner/scripts/build-r-ecs-images.sh)

## Cutover Sequence

### 1. Stop Active R Scans

Make sure no R Step Functions execution or ad hoc R worker process is still running.

Check:

```bash
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-east-1:<account-id>:stateMachine:package-scanner-dev-r-scan-orchestrator \
  --status-filter RUNNING \
  --region us-east-1 \
  --profile <aws-profile>
```

If needed, stop active executions before cutover.

### 2. Deploy the ECS Stack Skeleton

This creates:
- ECS clusters
- launch templates
- Auto Scaling groups
- ECR repositories
- ECS task definitions
- new R Step Functions state machine

Run:

```bash
./scripts/deploy-r-ecs-cfn.sh \
  --stack-name cyber-scanner-dev \
  --environment-name package-scanner-dev \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <account-id> \
  --deployment-lock-token <lock-token> \
  --s3-bucket package-scanner-dev-scan-evidence-<account-id>-us-east-1
```

Record these outputs:
- `RLinuxRepositoryUri`
- `RWindowsRepositoryUri`
- `RScanOrchestrationStateMachineArn`

### 3. Build and Push the Linux R Image

Run from Linux:

```bash
./scripts/build-r-ecs-images.sh \
  --stack-name cyber-scanner-dev \
  --region us-east-1 \
  --profile <aws-profile>
```

This publishes the Linux amd64 image to:
- `RLinuxRepositoryUri:latest`

### 4. Build and Push the Windows R Image

Run from a Windows Docker host with access to the repo and AWS credentials.

Resolve the repo URI:

```powershell
$repo = aws cloudformation describe-stacks `
  --stack-name cyber-scanner-dev `
  --query "Stacks[0].Outputs[?OutputKey=='RWindowsRepositoryUri'].OutputValue | [0]" `
  --output text `
  --region us-east-1 `
  --profile <aws-profile>
```

Log in to ECR:

```powershell
$account = aws sts get-caller-identity --query Account --output text --region us-east-1 --profile <aws-profile>
aws ecr get-login-password --region us-east-1 --profile <aws-profile> |
  docker login --username AWS --password-stdin "$account.dkr.ecr.us-east-1.amazonaws.com"
```

Build and push:

```powershell
docker build -f docker/r-windows.Dockerfile -t "$repo:latest" .
docker push "$repo:latest"
```

### 5. Verify Worker Capacity

Confirm one instance is registered in each expected worker pool:

```bash
aws ecs list-container-instances \
  --cluster package-scanner-dev-r-linux \
  --region us-east-1 \
  --profile <aws-profile>

aws ecs list-container-instances \
  --cluster package-scanner-dev-r-windows \
  --region us-east-1 \
  --profile <aws-profile>
```

Expected:
- Linux cluster has one instance total
  - one x86_64
- Windows cluster has one instance

### 6. Start a Small Validation Run

Use the small R test lockfile first.

```bash
./scripts/start-r-scan.sh \
  --stack-name cyber-scanner-dev \
  --input-bucket package-scanner-dev-scan-input-<account-id>-us-east-1 \
  --input-object-key inputs/r/renv.lock \
  --source-lock-file ./artifacts/renv.test.lock \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <account-id> \
  --deployment-lock-token <lock-token> \
  --remediate-medium true \
  --fail-on-medium false \
  --remediate-unknown true \
  --fail-on-unknown false
```

Validate:
- Step Functions execution succeeds
- Linux amd64 publishes evidence
- offline bundle artifacts exist

### 7. Run the Full R Lockfile

After the test run passes:

```bash
./scripts/start-r-scan.sh \
  --stack-name cyber-scanner-dev \
  --input-bucket package-scanner-dev-scan-input-<account-id>-us-east-1 \
  --input-object-key inputs/r/renv.lock \
  --source-lock-file ./artifacts/renv.full.lock \
  --region us-east-1 \
  --profile <aws-profile> \
  --expected-account-id <account-id> \
  --deployment-lock-token <lock-token> \
  --remediate-medium true \
  --fail-on-medium false \
  --remediate-unknown true \
  --fail-on-unknown false
```

### 8. Observe Final Evidence

Primary summary:

```text
s3://<evidence-bucket>/evidence/orchestration/r/<execution-id>/orchestration-summary.json
```

Per-platform summaries:

```text
s3://<evidence-bucket>/evidence/traceability/r/<platform>/<timestamp>/materialization-summary.json
s3://<evidence-bucket>/evidence/traceability/r/<platform>/<timestamp>/governance-summary.json
```

Offline bundles:

```text
s3://<evidence-bucket>/evidence/packages/offline/r/<platform>/<timestamp>/
```

## Rollback Point

Rollback is safest before Step 7.

If the small validation run fails:
- do not promote ECS as authoritative
- keep or restore the previous CodeBuild-based R stack template
- redeploy the old stack with [deploy-cfn.sh](/home/tjansto/github/natgo/package-scanner/scripts/deploy-cfn.sh)

If the full ECS R run fails after validation:
- keep the ECS stack for troubleshooting
- do not start Python migration yet
- do not remove repo references to the old CodeBuild implementation until root cause is known

## Notes

- The current implementation does not require a custom AMI.
- Host customization can be introduced later if needed.
- The primary optimization is prebaking tooling into the container images, not the host image.
