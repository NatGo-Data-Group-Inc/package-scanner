# AWS Support Case: CodeBuild Returning 45-Minute Build Timeout Despite 480-Minute Project and Override Settings

## Summary

We have three AWS CodeBuild projects in `us-east-1` that are configured for `timeoutInMinutes = 480` and `queuedTimeoutInMinutes = 480`, but every build started against those projects is created with an effective build timeout of `45` minutes.

This happens for:

- Step Functions initiated builds using `arn:aws:states:::codebuild:startBuild.sync`
- direct `aws codebuild start-build`
- direct `aws codebuild start-build --timeout-in-minutes-override 480`

The result is that long-running R environment materialization builds terminate in `BUILD_TIMED_OUT` after about 45 minutes even though both the project configuration and explicit API override request 480 minutes.

## Account and Region

- AWS account: `807497180525`
- Region: `us-east-1`
- Stack name: `cyber-scanner-dev`

## Affected CodeBuild Projects

- `package-scanner-dev-r-scan-linux-amd64`
- `package-scanner-dev-r-scan-linux-arm64`
- `package-scanner-dev-r-scan-windows-amd64`

## Expected Behavior

Each build should run with:

- `timeoutInMinutes = 480`
- `queuedTimeoutInMinutes = 480`

This should be true when the build is launched from the project definition and also when `StartBuild` explicitly requests `timeoutInMinutesOverride = 480`.

## Actual Behavior

Each build object is created with:

- `timeoutInMinutes = 45`
- `queuedTimeoutInMinutes = 480`

This causes long-running builds to fail at about 45 minutes with `BUILD_TIMED_OUT`.

## Evidence: Live Project Configuration

Queried with:

```bash
aws codebuild batch-get-projects \
  --names \
    package-scanner-dev-r-scan-linux-amd64 \
    package-scanner-dev-r-scan-linux-arm64 \
    package-scanner-dev-r-scan-windows-amd64 \
  --region us-east-1 \
  --profile AdministratorAccess-807497180525 \
  --query 'projects[].{name:name,timeoutInMinutes:timeoutInMinutes,queuedTimeoutInMinutes:queuedTimeoutInMinutes,lastModified:lastModified}'
```

Returned:

```json
[
  {
    "name": "package-scanner-dev-r-scan-linux-amd64",
    "timeoutInMinutes": 480,
    "queuedTimeoutInMinutes": 480,
    "lastModified": "2026-03-16T14:09:27.536000-05:00"
  },
  {
    "name": "package-scanner-dev-r-scan-linux-arm64",
    "timeoutInMinutes": 480,
    "queuedTimeoutInMinutes": 480,
    "lastModified": "2026-03-16T14:09:27.269000-05:00"
  },
  {
    "name": "package-scanner-dev-r-scan-windows-amd64",
    "timeoutInMinutes": 480,
    "queuedTimeoutInMinutes": 480,
    "lastModified": "2026-03-16T14:09:27.092000-05:00"
  }
]
```

## Evidence: No Timeout-Related Service Quota Is Visible

We also checked Service Quotas for CodeBuild in `us-east-1`:

```bash
aws service-quotas list-service-quotas \
  --service-code codebuild \
  --region us-east-1 \
  --profile AdministratorAccess-807497180525 \
  --query 'Quotas[?contains(QuotaName, `timeout`) || contains(QuotaName, `Timeout`) || contains(Description, `timeout`) || contains(Description, `Timeout`)]'
```

Returned:

```json
[]
```

There is no visible timeout-related Service Quota explaining the effective `45` minute runtime limit.

## Evidence: Step Functions Does Not Override Timeout

The live state machine definition for `package-scanner-dev-r-scan-orchestrator` uses:

- `arn:aws:states:::codebuild:startBuild.sync`
- `ProjectName`
- `EnvironmentVariablesOverride`

It does not pass any timeout override fields.

## Evidence: Full Run Builds Still Created with 45-Minute Timeout

Step Functions execution:

- `r-scan-20260316T191012Z-75bf44ef`

Failed child builds:

```json
[
  {
    "id": "package-scanner-dev-r-scan-linux-amd64:422f7851-0571-4d8e-890f-4bb2184faf13",
    "projectName": "package-scanner-dev-r-scan-linux-amd64",
    "initiator": "states/package-scanner-dev-r-scan-orchestrator",
    "timeoutInMinutes": 45,
    "queuedTimeoutInMinutes": 480,
    "startTime": "2026-03-16T14:10:13.178000-05:00",
    "endTime": "2026-03-16T14:55:25.016000-05:00",
    "buildStatus": "FAILED",
    "currentPhase": "COMPLETED"
  },
  {
    "id": "package-scanner-dev-r-scan-linux-arm64:5eb7c4a9-2ac4-4aee-ae3c-ee618b3b02cd",
    "projectName": "package-scanner-dev-r-scan-linux-arm64",
    "initiator": "states/package-scanner-dev-r-scan-orchestrator",
    "timeoutInMinutes": 45,
    "queuedTimeoutInMinutes": 480,
    "startTime": "2026-03-16T14:10:13.211000-05:00",
    "endTime": "2026-03-16T14:55:28.011000-05:00",
    "buildStatus": "FAILED",
    "currentPhase": "COMPLETED"
  },
  {
    "id": "package-scanner-dev-r-scan-windows-amd64:deee8ad1-b6e6-4d4b-a3aa-d2e530498976",
    "projectName": "package-scanner-dev-r-scan-windows-amd64",
    "initiator": "states/package-scanner-dev-r-scan-orchestrator",
    "timeoutInMinutes": 45,
    "queuedTimeoutInMinutes": 480,
    "startTime": "2026-03-16T14:10:13.215000-05:00",
    "endTime": "2026-03-16T14:56:06.566000-05:00",
    "buildStatus": "FAILED",
    "currentPhase": "COMPLETED"
  }
]
```

## Evidence: Direct StartBuild Also Returns 45 Minutes

Manual test:

```bash
aws codebuild start-build \
  --project-name package-scanner-dev-r-scan-linux-amd64 \
  --region us-east-1 \
  --profile AdministratorAccess-807497180525
```

Returned build:

- `package-scanner-dev-r-scan-linux-amd64:b62a511b-f160-431a-9330-bfc119b63ad0`
- `timeoutInMinutes = 45`

This isolates the issue away from Step Functions.

## Evidence: Explicit Timeout Override Is Ignored

Manual test with explicit override:

```bash
aws codebuild start-build \
  --project-name package-scanner-dev-r-scan-linux-amd64 \
  --timeout-in-minutes-override 480 \
  --region us-east-1 \
  --profile AdministratorAccess-807497180525
```

AWS CLI `--debug` showed the exact request body sent to CodeBuild:

```json
{
  "projectName": "package-scanner-dev-r-scan-linux-amd64",
  "timeoutInMinutesOverride": 480
}
```

CodeBuild still returned a build object with:

- build ID: `package-scanner-dev-r-scan-linux-amd64:13755ecd-633b-4b96-990e-ab426e2523b6`
- `timeoutInMinutes = 45`

The same debug session also showed the service request ID:

- `x-amzn-RequestId: eb9a59b0-57eb-4ee1-be50-599f5e9d7f22`

## Evidence: Fresh Minimal Projects Also Reproduce the Problem

To rule out drift or corruption in the original scanner projects, we created brand-new minimal `NO_SOURCE` CodeBuild projects with a trivial buildspec in two regions:

- `timeout-probe-us-east-1-20260317T155940Z`
- `timeout-probe-us-west-2-20260317T155940Z`

Both projects were created with:

- `timeoutInMinutes = 120`
- `queuedTimeoutInMinutes = 480`
- image: `aws/codebuild/standard:7.0`
- compute type: `BUILD_GENERAL1_SMALL`

### Fresh Project in `us-east-1`

Project creation returned:

- project name: `timeout-probe-us-east-1-20260317T155940Z`
- `timeoutInMinutes = 120`
- `queuedTimeoutInMinutes = 480`

Started builds:

1. Without override:
   - build ID: `timeout-probe-us-east-1-20260317T155940Z:66777a80-a2ef-41aa-a79c-3d04aab7439a`
   - returned `timeoutInMinutes = 45`
2. With explicit `--timeout-in-minutes-override 120`:
   - build ID: `timeout-probe-us-east-1-20260317T155940Z:c2b62179-b8d9-4c9f-a63e-4a898dd4c2fc`
   - returned `timeoutInMinutes = 45`

### Fresh Project in `us-west-2`

Project creation returned:

- project name: `timeout-probe-us-west-2-20260317T155940Z`
- `timeoutInMinutes = 120`
- `queuedTimeoutInMinutes = 480`

Started builds:

1. Without override:
   - build ID: `timeout-probe-us-west-2-20260317T155940Z:416b2a18-227a-4cc9-8d1a-9fe7f9543eb3`
   - returned `timeoutInMinutes = 45`
2. With explicit `--timeout-in-minutes-override 120`:
   - build ID: `timeout-probe-us-west-2-20260317T155940Z:c83036e5-3f94-48f8-a1db-c9a53fda62f7`
   - returned `timeoutInMinutes = 45`

All four probe builds were then stopped, and both temporary projects were deleted.

## Why This Appears To Be a Service-Side Problem

All of these are simultaneously true:

1. The deployed project objects report `timeoutInMinutes = 480`.
2. The Step Functions state machine does not override timeout.
3. A direct `StartBuild` without override returns `timeoutInMinutes = 45`.
4. A direct `StartBuild` with `timeoutInMinutesOverride = 480` still returns `timeoutInMinutes = 45`.
5. Brand-new minimal projects in `us-east-1` and `us-west-2` configured for `120` also return `timeoutInMinutes = 45`.
6. No timeout-related Service Quota is visible for CodeBuild in `us-east-1`.

Because the explicit `StartBuild` request body included the override and the service response still created a `45` minute build, this does not appear to be caused by local CLI construction, CloudFormation template content, or Step Functions orchestration.

## Business Impact

These projects are used to materialize and validate large R environments for offline transfer. The intended workloads take multiple hours. The unexpected `45` minute limit makes the materialization pipeline non-functional for real workloads and causes deterministic build failures.

## Request to AWS Support

Please investigate why CodeBuild in this account and region is creating builds with `timeoutInMinutes = 45` for these projects even though:

- the projects are configured at `480`
- `StartBuild` can be observed receiving `timeoutInMinutesOverride = 480`

We have now also confirmed the same behavior on brand-new minimal CodeBuild projects in both `us-east-1` and `us-west-2`, configured for `120`, with and without explicit `120` overrides.

Please confirm whether:

- there is a service-side constraint or hidden account-level override being applied
- the projects are affected by a regional or account-specific issue
- there is any backend state drift between the stored project definition and runtime build creation
- there is an account-level backend policy affecting CodeBuild timeout enforcement across multiple regions

## Reference Documentation

- Project timeout limits:
  `https://docs.aws.amazon.com/codebuild/latest/APIReference/API_Project.html`
- StartBuild timeout override:
  `https://docs.aws.amazon.com/codebuild/latest/APIReference/API_StartBuild.html`
