# Operations Runbook

## Daily Operations

1. Validate AWS identity and region:
   - `aws sts get-caller-identity --profile <profile>`
2. Confirm stack status:
   - `aws cloudformation describe-stacks --stack-name <stack> --query "Stacks[0].StackStatus"`
3. Submit scans (Python/R) per workflow.
4. Verify governance outputs in evidence bucket.

## Standard Procedures

### Deploy/Update

- Use `scripts/deploy-cfn.sh` with:
  - `--profile`
  - `--expected-account-id`
  - `--deployment-lock-token`

### Start Python Scan

- Use `scripts/start-python-scan.sh`.
- Ensure `environment.yml` is uploaded first.

### Start R Scan

- Use `scripts/start-r-scan.sh`.
- Ensure `renv.lock` is uploaded first.

## Incident Procedure

If scans fail unexpectedly:

1. Capture failing build IDs from script output.
2. Pull CodeBuild logs:
   - `aws codebuild batch-get-builds --ids <build-id>`
3. Classify failure:
   - guardrail/config mismatch
   - tool/runtime failure
   - governance gate failure
4. Use `docs/troubleshooting.md` for remediation.
5. Re-run after corrective action.

## Change Management

For production updates:

1. Validate changes in non-prod environment first.
2. Record:
   - stack name
   - region/account
   - deployment lock token
   - template/script commit hash
3. Deploy through approved profile/account.
4. Run one Python and one R validation scan.

## Rollback Guidance

- Re-deploy previous known-good template/scripts with same lock token.
- Validate stack outputs and rerun smoke scans.
- Confirm evidence artifact paths and governance outputs restore expected behavior.

## Evidence Retention Checks

- Evidence bucket should retain long-term artifacts.
- Ephemeral bucket should purge by lifecycle policy and explicit cleanup.
- Verify lifecycle policy in bucket configuration after deploy updates.

