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

