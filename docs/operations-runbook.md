# Operations Runbook

Use [handoff-runbook.md](./handoff-runbook.md) as the primary day-to-day operator guide. This file is the shorter operational checklist.

## Daily Operations

1. Validate AWS identity and region:
   - `aws sts get-caller-identity --profile <profile>`
2. Confirm stack status:
   - `aws cloudformation describe-stacks --stack-name <stack> --query "Stacks[0].StackStatus"`
3. Submit scans (Python/R) per workflow.
4. Verify governance outputs in evidence bucket.
5. For R, verify Step Functions execution status before reviewing per-platform artifacts.
6. For enclave deliveries, pull the latest platform cache bundles and `.sha256` files from `evidence/packages/offline/r/<platform>/<timestamp>/`.

## Standard Procedures

### Deploy/Update

- Use `scripts/deploy-cfn.sh` with:
  - `--profile`
  - `--expected-account-id`
  - `--deployment-lock-token`

### Start Python Scan

- Use `scripts/start-python-scan.sh`.
- Ensure `environment.yml` is uploaded first.

### S3 Layout and Artifact Map

Buckets (dev):
- Input: `package-scanner-dev-scan-input-<acct>-<region>`
- Evidence (long-term): `package-scanner-dev-scan-evidence-<acct>-<region>`
- Ephemeral (short-lived build outputs): `package-scanner-dev-scan-ephemeral-<acct>-<region>`

Python artifacts (per platform/timestamp in evidence bucket):
- Requirements: `evidence/requirements/python/<platform>/<ts>/environment.yml` (and resolved lockfile if present)
- Model results: `evidence/model-results/python/<platform>/<ts>/trivy-sbom-report.json` (plus `safety-report.json` if enabled)
- Governance: `evidence/governance/python/<platform>/<ts>/vulnerability-findings.csv`, `remediation-required.csv`, `remediation-exceptions.csv`, `remediation-spreadsheet.csv`, `governance-summary.json`
- Traceability: `evidence/traceability/python/<platform>/<ts>/run-metadata.json`, `materialization-summary.json`

R artifacts (per platform/timestamp in evidence bucket):
- Requirements: `evidence/requirements/r/<platform>/<ts>/renv.lock`, `installed-packages.csv`
- Model results: `evidence/model-results/r/<platform>/<ts>/osv-report.json`, `trivy-sbom-report.json`
- Governance: `evidence/governance/r/<platform>/<ts>/vulnerability-findings.csv`, `remediation-required.csv`, `remediation-exceptions.csv`, `remediation-spreadsheet.csv`, `governance-summary.json`
- Traceability: `evidence/traceability/r/<platform>/<ts>/run-metadata.json`, `materialization-summary.json`
- Offline caches: `evidence/packages/offline/r/<platform>/<ts>/renv-cache-*.tar.gz` (+ `.sha256`)

Enclave delivery (Python): pull the Python evidence set above, plus the original `environment.yml` and any offline wheel/conda cache if produced; apply the same approval/transfer flow as R.

### Start R Scan

- Use `scripts/start-r-scan.sh`.
- Ensure `renv.lock` is uploaded first, or pass `--source-lock-file`.
- For named approval candidates, upload and run the specific candidate lockfile path instead of relying only on `inputs/r/renv.lock`.
  - Example: `inputs/r/candidates/PI-26.3/linux-amd64/<timestamp>/renv.lock`
- Each successful R workflow now:
  - plans staged restore batches
  - runs sequential CodeBuild stages per platform
  - restores the environment
  - emits materialization evidence
  - publishes the cache archive + `.sha256` file to `evidence/packages/offline/r/<platform>/<timestamp>/`

### Refreshing R ECS image without cache issues

- Build/push with an immutable tag (timestamp or commit hash).
- Register a new task-definition revision pointing at that tag so ECS won’t reuse a cached `:latest`:

```bash
IMAGE_TAG=$(date -u +%Y%m%dT%H%M%SZ)
docker build -t 807497180525.dkr.ecr.us-east-1.amazonaws.com/package-scanner-dev/r-scan-linux:${IMAGE_TAG} -f docker/r-linux.Dockerfile .
docker push 807497180525.dkr.ecr.us-east-1.amazonaws.com/package-scanner-dev/r-scan-linux:${IMAGE_TAG}

scripts/register-r-task-def.sh \
  --image 807497180525.dkr.ecr.us-east-1.amazonaws.com/package-scanner-dev/r-scan-linux:${IMAGE_TAG} \
  --region us-east-1 \
  --profile AdministratorAccess-807497180525
```

The R orchestrator targets the latest revision of the `package-scanner-dev-r-linux-amd64` family, so the next run pulls the new image. Ensure the ECS instance role retains `ecr:GetAuthorizationToken` so pulls succeed.

Operator impact:

- This is a required step whenever the scanner code changes and the runtime behavior must match the repo for governance reasons.
- Do not assume a rebuilt image is active until you confirm the ECS task is running on the new task-definition revision and image digest.

### Current R ECS behavior changes

- The live ECS orchestrators now use task-definition family ARNs instead of revision-pinned ARNs.
- Failed R ECS runs now publish `restore-root-cause.txt` and the dashboard surfaces that root cause in the latest-failed section.
- Repository prechecks only validate `Source: Repository` lockfile entries against CRAN/RSPM visibility.
- GitHub-sourced lockfile entries are no longer incorrectly rejected by the pre-restore availability check.
- The restore path retries still-missing requested packages once after the first `renv::restore()` pass to recover from dependency-ordering failures.

User-facing runbook effects:

- `docs/workflow.md`
  - start scans using the exact candidate lockfile path that is intended for approval
  - understand that new task-definition revisions are now picked up automatically by ECS orchestrators
- `docs/troubleshooting.md`
  - use `restore-root-cause.txt` as the first triage artifact for R ECS failures
  - verify the actual ECS task definition/image before assuming a runtime fix is active

### Enclave handoff (R)

For an approved run, collect these artifacts from the evidence bucket:
- `renv.lock` used for the run.
- Offline cache tarball + checksum: `evidence/packages/offline/r/<platform>/<ts>/renv-cache.tar.gz` and `.sha256`.
- Requirements snapshot: `installed-packages.csv`.
- Governance outputs: `vulnerability-findings.csv`, `remediation-required.csv`, `remediation-exceptions.csv`, `remediation-spreadsheet.csv`, `governance-summary.json`.
- Model reports: `osv-report.json`, `trivy-sbom-report.json`.
- Run metadata and materialization summary: `run-metadata.json`, `materialization-summary.json`.

Enclave steps (offline):
- Verify the `.sha256`, place cache, set `RENV_PATHS_CACHE`.
- Ensure R 4.4.0 matches scanner; run `renv::restore()` using the approved `renv.lock`.
- Provide governance/model artifacts to cyber for evidence and sign-off.

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

For R failures, inspect both:

- the Step Functions execution status
- the failing platform/stage CodeBuild build ID

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

Artifact locations (R linux):
- Evidence bucket: `s3://package-scanner-dev-scan-evidence-<acct>-<region>`
- Offline cache: `evidence/packages/offline/r/linux-amd64/<timestamp>/renv-cache-linux-amd64-<timestamp>.tar.gz` (+ `.sha256`)
- Lockfile: `evidence/requirements/r/linux-amd64/<timestamp>/renv.lock`
- Governance/model: `evidence/governance/r/linux-amd64/<timestamp>/...` (findings/remediation CSVs, governance-summary.json), `evidence/model-results/r/linux-amd64/<timestamp>/osv-report.json`, `trivy-sbom-report.json`
- Traceability: `evidence/traceability/r/linux-amd64/<timestamp>/run-metadata.json`, `materialization-summary.json`, `installed-packages.csv`

Transfer steps (airgapped R):
1. Pull the lockfile, cache tarball, `.sha256`, and governance/model/traceability bundle from the evidence bucket.
2. Deliver the artifact bundle to cyber and obtain approval before any enclave install.
3. After approval, move the approved artifacts via the sanctioned transfer path.
4. In the enclave (per platform):
   - Linux cache path example: `/opt/renv/cache`; Windows cache path example: `C:\renv\cache`.
   - Untar cache: Linux `tar -xzf renv-cache-linux-amd64-<timestamp>.tar.gz -C /`; Windows use 7zip/PowerShell to extract into `C:\`.
   - Set cache env: Linux `export RENV_PATHS_CACHE=/opt/renv/cache`; Windows `set RENV_PATHS_CACHE=C:\renv\cache`.
   - Ensure R 4.4.0 is installed and on PATH.
   - Restore:
     - Linux:
       ```bash
       R -q <<'RSCRIPT'
       options(repos = c(CRAN = "https://cloud.r-project.org"))
       renv::restore(lockfile = "renv.lock", prompt = FALSE, clean = TRUE)
       RSCRIPT
       ```
     - Windows (PowerShell):
       ```powershell
       $env:RENV_PATHS_CACHE="C:\renv\cache"
       Rscript -e "options(repos=c(CRAN='https://cloud.r-project.org')); renv::restore(lockfile='renv.lock', prompt=FALSE, clean=TRUE)"
       ```
   - Confirm library path from `renv/library-path.txt`; default is `~/.local/share/renv/library` (Linux) or `%USERPROFILE%\\AppData\\Local\\renv\\library` (Windows).
