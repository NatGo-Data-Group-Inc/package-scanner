# Operations Runbook

## Daily Operations

1. Validate AWS identity and region:
   - `aws sts get-caller-identity --profile <profile>`
2. Confirm stack status:
   - `aws cloudformation describe-stacks --stack-name <stack> --query "Stacks[0].StackStatus"`
3. Submit scans (Python/R) per workflow.
4. Verify governance outputs in evidence bucket.
5. For enclave deliveries, pull the latest platform cache bundles and `.sha256` files from `evidence/packages/offline/r/<platform>/<timestamp>/`.

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
- Ensure `renv.lock` is uploaded first, or pass `--source-lock-file`.
- Each successful R build now restores the environment, emits materialization evidence, and publishes the cache archive + `.sha256` file to `evidence/packages/offline/r/<platform>/<timestamp>/`.

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

## Storage Requirements

- CodeBuild’s default NVMe scratch volume must have at least 15 GB free before each scan. Use `df -h` (Linux) or `Get-PSDrive C` (Windows) in troubleshooting steps.
- Keep only the most recent `renv`/conda cache on disk. Once an archive is uploaded to S3, delete the local cache (`rm -rf ~/.local/share/renv/cache/*` or `Remove-Item -Recurse`) to reclaim space.
- If you consistently exceed scratch capacity, update the stack to attach a larger EBS volume and mount it at `/mnt/package_cache` (Linux) or `G:\package_cache` (Windows), then point `RENV_PATHS_CACHE` there.

## Rollback Guidance

- Re-deploy previous known-good template/scripts with same lock token.
- Validate stack outputs and rerun smoke scans.
- Confirm evidence artifact paths and governance outputs restore expected behavior.

## Evidence Retention Checks

- Evidence bucket should retain long-term artifacts.
- Ephemeral bucket should purge by lifecycle policy and explicit cleanup.
- Verify lifecycle policy in bucket configuration after deploy updates.

## Enclave Transfer Checklist

1. Pull the latest passing `renv.lock` and cache archive (plus `.sha256`) from the evidence bucket.
2. Verify the checksum locally: `sha256sum -c renv-cache-<platform>.tar.gz.sha256`.
3. Move the files across the approved transfer mechanism.
4. Inside the enclave, unpack to the designated cache path, set `RENV_PATHS_CACHE`, install R 4.4.0, and run `renv::restore()` to hydrate the environment without outbound access.
