# Troubleshooting Guide

This guide covers common failure modes and exact remediations.

## 1) Guardrail Failures

### Error: `--profile is required unless --allow-default-profile is explicitly set`

Cause:

- Command did not pass `--profile` and default profile usage is blocked.

Fix:

- Add `--profile <aws-profile>`, or intentionally add `--allow-default-profile`.

### Error: `account mismatch. expected=... actual=...`

Cause:

- Current AWS identity does not match `--expected-account-id`.

Fix:

1. Confirm caller identity:
   - `aws sts get-caller-identity --profile <aws-profile>`
2. Use correct AWS profile/account.
3. Re-run with correct `--expected-account-id`.

### Error: `deployment lock token mismatch`

Cause:

- Stack tag `DeploymentLockToken` is different from provided token.

Fix:

1. Inspect stack tags:
   - `aws cloudformation describe-stacks --stack-name <stack> --query "Stacks[0].Tags" --output table`
2. Re-run with matching `--deployment-lock-token`.
3. If token rotation is required, perform controlled stack update with approved token change.

### Error: `stack <name> is not lock-tagged`

Cause:

- Stack was created before lock-tag guardrail adoption.

Fix:

- Redeploy stack with `scripts/deploy-cfn.sh` including `--deployment-lock-token`.

## 2) Deployment Failures

### Error: `Template not found`

Cause:

- Wrong working directory or missing repository files.

Fix:

- Run from repository root and confirm `deployment/cfn/python-scan-stack.yaml` exists.

### Error during `cloudformation deploy` about IAM capability

Cause:

- Stack includes IAM resources.

Fix:

- Use deploy script (already includes `--capabilities CAPABILITY_NAMED_IAM`).

### Error: governance script upload cannot resolve evidence bucket output

Cause:

- Stack failed or outputs are unavailable.

Fix:

1. Check stack status:
   - `aws cloudformation describe-stacks --stack-name <stack> --query "Stacks[0].StackStatus"`
2. Resolve failed resources in CloudFormation events.
3. Re-run deploy after correction.

## 3) Scan Start Failures

### Error: `Missing stack output: <ProjectNameOutput>`

Cause:

- Stack does not contain expected CodeBuild project output (outdated stack/template).

Fix:

- Re-run deployment to update stack to latest template.

### Error: input object not found

Cause:

- Input file not uploaded to expected key.

Fix:

- Python: upload to `inputs/python/environment.yml` (or pass explicit `--input-object-key`).
- R: upload to `inputs/r/renv.lock` (or pass explicit `--input-object-key`).

### Error: evidence/ephemeral bucket output not found

Cause:

- Stack output missing due deploy drift or failed stack.

Fix:

- Re-deploy stack and validate outputs table.

## 4) Build Runtime Failures

### Trivy version mismatch

Cause:

- Downloaded Trivy binary does not match expected pinned version.

Fix:

- Re-run deploy (template pins version and verifies).
- Check outbound internet access from CodeBuild environment.

### Governance parser error (`governance-error: ...`)

Cause:

- Required scanner artifact missing/invalid schema.

Fix:

1. Inspect CodeBuild logs for preceding scanner step failures.
2. Validate generated JSON report files are non-empty and valid.
3. Re-run scan after fixing input/package resolution issues.

### Governance gate failure (exit code `3`)

Cause:

- Findings violate configured gate policy (`fail_on_medium`, high/critical findings).

Fix:

1. Review:
   - `vulnerability-findings.csv`
   - `remediation-required.csv`
   - `governance-summary.json`
2. Remediate package versions or process approved exceptions.
3. Re-run scan.

### CodeBuild timeout mismatch

Cause:

- The live CodeBuild project timeout and the timeout actually applied to started builds do not match.

Fix:

1. Run the timeout diagnostic:
   - `./scripts/diagnose-codebuild-timeouts.sh --project-name package-scanner-dev-r-scan-linux-amd64 --region us-east-1 --profile AdministratorAccess-807497180525 --include-no-override --override-timeout 60 --override-timeout 120 --override-timeout 480 --output-file /tmp/codebuild-timeout-report.json`
2. Inspect the report for:
   - project `timeout_in_minutes`
   - each probe build's returned `timeout_in_minutes`
3. By default the script stops the probe builds after the timeout value is confirmed.
4. If the project reports one timeout and the started builds still come back with another, treat it as an AWS-side issue and attach the report to the support case.

### R stage exceeds the effective 45-minute runtime

Cause:

- The current AWS account behavior still enforces an effective 45-minute CodeBuild runtime, so a stage that restores too many packages can time out.

Fix:

1. Lower the R stage size:
   - rerun with `--r-stage-package-count 10`
   - if still too large, reduce further
2. Restart the R scan with the same `renv.lock`.
3. Confirm the new stage 1 build includes the smaller `STAGE_PACKAGES_JSON` payload in the build environment.

### R ECS scan used an old image / old task definition

Cause:

- A Step Functions ECS state machine was pinned to a revisioned task-definition ARN instead of the task-definition family ARN.

Fix:

1. Verify the live state machine definition references the family ARN:
   - `arn:aws:ecs:<region>:<account>:task-definition/<family>`
   - not `...:task-definition/<family>:<revision>`
2. Register the new scanner image with a new task-definition revision:
   - `scripts/register-r-task-def.sh --image <ecr-uri:tag> ...`
3. Re-run the scan. The next run should resolve the latest active revision automatically.

Operator impact:

- This affects ECS-based R and Python scan orchestrators.
- If a scan behaves as if a recent image fix is missing, confirm the task-definition ARN on the actual ECS task before debugging package behavior.

### R restore root cause says package is unavailable before restore

Cause:

- The lockfile requested a repository-sourced package that is not visible from CRAN/RSPM at run time, or the lockfile still contains stale package entries that are no longer intended.

Fix:

1. Pull the run root cause file:
   - `evidence/traceability/r/<platform>/<timestamp>/restore-root-cause.txt`
2. Compare the run lockfile in S3 with the local candidate lockfile.
3. Remove unintended stale package entries from the candidate lockfile, or update the candidate package set and regenerate the lockfile.
4. Upload the corrected candidate lockfile and rerun.

Operator impact:

- This affects candidate preparation for named R runs such as `PI-26.3`.
- The canonical scan input may differ from the local file you were editing; always compare against the S3 object actually used by the run.

### R restore says dependency was unavailable even though it built later

Cause:

- `renv::restore()` can fail a package early if one of its dependencies is not yet available in the target library at that moment, even if that dependency finishes building later in the same run.

Fix:

1. Confirm the package and dependency both appear in `restore.log`.
2. Verify the scanner image includes the retry-enabled restore path.
3. Rebuild/push/register the latest image if the running task is on an older revision.

Operator impact:

- This is the failure mode that affected packages like `arkdb` and `progress`.
- The retry-enabled scanner image (`retry-*` tags and later revisions) is required to recover from this class of transient ordering issue.

### Windows R stage failed during Rtools installation

Cause:

- External download failure or transient network problem while obtaining the Rtools installer.

Fix:

1. Confirm the build is using the helper-based installer path, not the old inline download.
2. Re-run the scan after deployment if the stack was recently updated.
3. Check whether the installer is now being pulled from the evidence bucket cache path.

## 5) Output Discovery Issues

If unsure where reports landed:

1. Find recent traceability metadata:
   - `s3://<evidence-bucket>/evidence/traceability/<ecosystem>/<platform>/`
2. Open latest timestamp directory and inspect:
   - `run-metadata.json`
   - `governance-summary.json`
3. Use that timestamp to navigate governance/model-results/requirements paths.

## 6) Known Environment Limitation

In some local Windows setups, `bash -n` can fail due Git Bash signal pipe permission errors. This does not necessarily indicate a script syntax issue in repository content; validate via wrapper execution path or alternate shell host.
