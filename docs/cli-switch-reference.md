# CLI Switch Reference

This document lists available switches and what each one is used for.

## `scripts/deploy-cfn.sh`

Usage:

```bash
./scripts/deploy-cfn.sh [options]
```

Switches:

- `--region <region>`
  - AWS region for deploy/queries.
- `--profile <profile>`
  - AWS CLI profile to use.
- `--allow-default-profile`
  - Allows execution without passing `--profile`.
- `--expected-account-id <account-id>`
  - Guardrail: fails if STS caller account is different.
- `--stack-name <name>`
  - CloudFormation stack name.
- `--environment-name <name>`
  - Prefix used in resource naming/tags.
- `--deployment-lock-token <token>`
  - Required lock token; used to prevent cross-environment mistakes.
- `--existing-input-bucket-name <bucket>`
  - Reuse an existing input bucket instead of creating one.
- `--existing-evidence-bucket-name <bucket>`
  - Reuse an existing evidence bucket.
- `--existing-ephemeral-bucket-name <bucket>`
  - Reuse an existing ephemeral bucket.
- `--build-timeout-minutes <minutes>`
  - CodeBuild timeout value.
- `--linux-compute-type <type>`
  - Python/R linux amd64 CodeBuild compute type.
- `--linux-arm-compute-type <type>`
  - Python/R linux arm64 CodeBuild compute type.
- `--windows-compute-type <type>`
  - Python/R windows amd64 CodeBuild compute type.
- `-h|--help`
  - Prints usage.

## `scripts/start-python-scan.sh`

Usage:

```bash
./scripts/start-python-scan.sh --stack-name <name> --input-bucket <bucket> [options]
```

Required:

- `--stack-name <name>`
- `--input-bucket <bucket>`

Optional:

- `--input-object-key <key>`
  - Input object key (default `inputs/python/environment.yml`).
- `--evidence-bucket <bucket>`
  - Override evidence bucket (otherwise from stack output).
- `--evidence-prefix <prefix>`
  - Evidence path root (default `evidence`).
- `--ephemeral-bucket <bucket>`
  - Override ephemeral bucket (otherwise from stack output).
- `--ephemeral-prefix <prefix>`
  - Ephemeral path root (default `deploy/tmp/python`).
- `--region <region>`
  - AWS region.
- `--profile <profile>`
  - AWS profile.
- `--allow-default-profile`
  - Allow no explicit profile.
- `--expected-account-id <account-id>`
  - Guardrail account check.
- `--deployment-lock-token <token>`
  - Required; must match stack tag.
- `--enable-fortify`
  - Enables optional Fortify command.
- `--fortify-command <command>`
  - Custom Fortify command executed during build.
- `--safety-api-key <key>`
  - Safety API key for authenticated vulnerability scans (set when using licensed accounts).
- `--remediate-medium <true|false>`
  - Include medium findings in remediation-required outputs.
- `--fail-on-medium <true|false>`
  - Gate-fail build on medium findings.
- `-h|--help`
  - Prints usage.

## `scripts/start-r-scan.sh`

Usage:

```bash
./scripts/start-r-scan.sh --stack-name <name> --input-bucket <bucket> [options]
```

Required:

- `--stack-name <name>`
- `--input-bucket <bucket>`

Optional:

- `--input-object-key <key>`
  - Input object key (default `inputs/r/renv.lock`).
- `--evidence-bucket <bucket>`
  - Override evidence bucket.
- `--evidence-prefix <prefix>`
  - Evidence path root (default `evidence`).
- `--ephemeral-bucket <bucket>`
  - Override ephemeral bucket.
- `--ephemeral-prefix <prefix>`
  - Ephemeral path root (default `deploy/tmp/r`).
- `--region <region>`
  - AWS region.
- `--profile <profile>`
  - AWS profile.
- `--allow-default-profile`
  - Allow no explicit profile.
- `--expected-account-id <account-id>`
  - Guardrail account check.
- `--deployment-lock-token <token>`
  - Required; must match stack tag.
- `--remediate-medium <true|false>`
  - Include medium findings in remediation-required outputs.
- `--fail-on-medium <true|false>`
  - Gate-fail build on medium findings.
- `-h|--help`
  - Prints usage.

## PowerShell Wrappers

Wrappers map to the same behavior:

- `scripts/deploy-cfn.ps1`
- `scripts/start-python-scan.ps1`
- `scripts/start-r-scan.ps1`

Parameter names are PowerShell-style (for example `-StackName`, `-InputBucket`, `-DeploymentLockToken`) but semantically equivalent to bash switches.

### `scripts/deploy-cfn.ps1` parameters

- `-Region`
- `-Profile`
- `-AllowDefaultProfile`
- `-ExpectedAccountId`
- `-StackName`
- `-EnvironmentName`
- `-DeploymentLockToken` (mandatory)
- `-ExistingInputBucketName`
- `-ExistingEvidenceBucketName`
- `-ExistingEphemeralBucketName`
- `-BuildTimeoutMinutes`
- `-LinuxComputeType`
- `-LinuxArmComputeType`
- `-WindowsComputeType`

### `scripts/start-python-scan.ps1` parameters

- `-StackName` (mandatory)
- `-InputBucket` (mandatory)
- `-InputObjectKey`
- `-EvidenceBucket`
- `-EvidencePrefix`
- `-EphemeralBucket`
- `-EphemeralPrefix`
- `-Region`
- `-Profile`
- `-AllowDefaultProfile`
- `-ExpectedAccountId`
- `-DeploymentLockToken` (mandatory)
- `-EnableFortify`
- `-FortifyCommand`
- `-RemediateMedium`
- `-FailOnMedium`

### `scripts/start-r-scan.ps1` parameters

- `-StackName` (mandatory)
- `-InputBucket` (mandatory)
- `-InputObjectKey`
- `-EvidenceBucket`
- `-EvidencePrefix`
- `-EphemeralBucket`
- `-EphemeralPrefix`
- `-Region`
- `-Profile`
- `-AllowDefaultProfile`
- `-ExpectedAccountId`
- `-DeploymentLockToken` (mandatory)
- `-RemediateMedium`
- `-FailOnMedium`
