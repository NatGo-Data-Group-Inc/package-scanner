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
- For ECS-backed scans, the launcher now performs a prescan capacity guard on
  the target worker ASG. If `MinSize` or `DesiredCapacity` is below `1`, the
  script raises it to `1` before starting the scan and waits for at least one
  active ECS container instance.
- If the source environment was updated on another machine and you need a new
  Python candidate for this pipeline, capture it from the realized Conda env
  rather than hand-editing a plain `conda env export`:

```bash
python3 scripts/build-candidate-from-env-artifacts.py \
  --name <candidate-name> \
  --env-prefix <conda-env-prefix> \
  --conda-bin conda \
  --prefer-conda-available \
  --target-subdir linux-64 \
  --output candidates/<candidate-name>.yml \
  --artifacts-dir artifacts/<candidate-name> \
  --artifacts-zip artifacts/<candidate-name>.zip
```

- `--prefer-conda-available` is the default operator choice for Python
  candidates. It keeps packages on the Conda side when the configured channels
  can satisfy them and only falls back to pip when Conda cannot.
- When you capture from a Windows environment for the Linux ECS pipeline, set
  `--target-subdir linux-64` so the Conda availability probe checks the Linux
  index rather than the local Windows subdir.
- The generated candidate YAML is a normalized package request for this
  pipeline, not an exact lockfile. If you need to reproduce the realized build
  exactly, scan the exported `environment.yml` directly instead of relying on
  the normalized candidate to preserve every build pin.
- For local YAML files under `candidates/`, the webapp also provides a
  `Candidates` page. Use that page to upload the selected YAML and start a
  Python ECS scan without manually pre-populating checkpoint or ephemeral
  paths.
- The candidate-start GUI path resolves the input, evidence, ephemeral buckets,
  and Linux ECS state machine from the Python ECS CloudFormation stack at submit
  time. The ephemeral bucket is used after the execution starts for checkpoints;
  it is not required to know about the scan before submission.
- The same `Candidates` page also traces scan history by starting artifact.
  It groups Python and R catalog records by `input_bucket`/`input_object_key`
  and adds recent Step Functions executions that have not published catalog
  records yet, so an operator can follow a YAML or lockfile from submission to
  its scan histories.
- For CPU-only MIP workers, Python ECS materialization normalizes GPU/CUDA-pinned
  candidate specs before solving and retains both the original YAML and the
  normalization log in the evidence bundle.

Current Python Linux runtime expectations:
- The active Linux scanner image is built on `ubuntu:24.04`, not Amazon Linux.
- The runtime exports `CONDA_OVERRIDE_GLIBC` from the live host glibc version.
- On `linux-amd64`, the runtime also exports
  `CONDA_OVERRIDE_ARCHSPEC=x86_64_v3` so Conda virtual-package resolution
  matches the worker class used in ECS.
- If an exported Conda environment requires a newer glibc or an
  `x86_64_v3` microarchitecture, verify the live ECS task is running on the
  refreshed Python image before debugging package metadata.

Refreshing the Python ECS runtime:

```bash
IMAGE_TAG=$(date -u +%Y%m%dT%H%M%SZ)-<suffix>
./scripts/build-python-ecs-images.sh \
  --stack-name cyber-scanner-dev-python-ecs \
  --profile AdministratorAccess-807497180525 \
  --platforms linux/amd64 \
  --tag "${IMAGE_TAG}" \
  --no-latest

./scripts/register-python-task-def.sh \
  --image 807497180525.dkr.ecr.us-east-1.amazonaws.com/package-scanner-dev/python-scan-linux:${IMAGE_TAG} \
  --region us-east-1 \
  --profile AdministratorAccess-807497180525 \
  --memory 16384
```

- The Python ECS orchestrator resolves the latest active revision of the
  `package-scanner-dev-python-linux-amd64` task-definition family.
- Do not assume a rebuilt image is active until the actual ECS task shows the
  new task-definition revision and image digest.
- When the Linux worker pool is only needed for ad hoc scans, you can set the
  Python Linux ASG back to `0` after the run completes to avoid idle EC2 cost.
  The next prescan launch will raise it back to at least `1` automatically.

### Deploy/Run Webapp

- Use `scripts/deploy-webapp-cfn.sh` to deploy the dedicated ECS Fargate
  operator webapp stack.
- By default the ALB is `HTTP` only on port `80`.
- If browser policy or enterprise security tooling upgrades requests to
  `HTTPS`, supply `--tls-certificate-arn <acm-certificate-arn>` during deploy
  so the ALB exposes `443` and redirects `80 -> 443`.
- The webapp stack can be updated independently of the scanner stacks.
- The hosted webapp uses `gunicorn` and boto3-backed AWS calls; browser issues
  should be debugged separately from ALB reachability by checking both:
  - `http://<alb-hostname>/healthz`
  - `Invoke-WebRequest` or `curl` from the client host

### S3 Layout and Artifact Map

Buckets (dev):
- Input: `package-scanner-dev-scan-input-<acct>-<region>`
- Evidence (long-term): `package-scanner-dev-scan-evidence-<acct>-<region>`
- Ephemeral (short-lived build outputs): `package-scanner-dev-scan-ephemeral-<acct>-<region>`

### Cleanup Failed Scan Artifacts

- Use the GUI `Cleanup Failed` page to preview or delete failed scan artifacts
  for cataloged `FAILED`, `TIMED_OUT`, or `ABORTED` runs.
- Preview is safe and shows the exact S3 objects/prefixes that would be removed.
- Delete requires typing the exact execution id. It removes evidence/catalog
  outputs and ephemeral checkpoints for that execution.
- The cleanup intentionally does not remove shared source input objects under
  the input bucket, because those files can be reused across reruns.
- The cleanup script now tolerates missing orchestration summaries for failed
  runs and can derive delete targets from the failed run catalog record before
  falling back to traceability discovery.
- CLI equivalent:
  `python scripts/cleanup-failed-s3-artifacts.py --ecosystem <r|python> --execution-id <execution-id> --evidence-bucket <bucket> --ephemeral-bucket <bucket> --evidence-prefix evidence --region us-east-1 --profile <profile> --write`

Python artifacts (per platform/timestamp in evidence bucket):
- Requirements: `evidence/requirements/python/<platform>/<ts>/environment.yml` (and resolved lockfile if present)
- Model results: `evidence/model-results/python/<platform>/<ts>/trivy-sbom-report.json` (plus `safety-report.json` if enabled)
- Governance: `evidence/governance/python/<platform>/<ts>/vulnerability-findings.csv`, `remediation-required.csv`, `remediation-exceptions.csv`, `remediation-spreadsheet.csv`, `governance-summary.json`
- Traceability: `evidence/traceability/python/<platform>/<ts>/run-metadata.json`, `materialization-summary.json`
- Offline/cache artifacts:
  - `evidence/packages/offline/python/<platform>/<ts>/python-pkgs-<platform>-<ts>.tar.gz`
  - `evidence/env-artifacts/python/<platform>/<ts>/python-env-<platform>-<ts>.tar.gz`

Python packaging semantics:

- `python-env-*.tar.gz` is now produced with `conda-pack`.
- Restore/reuse of that environment must run `conda-unpack` after extraction at the target prefix.
- This makes the Python environment bundle relocatable in a way the previous plain tar/extract approach was not.
- `python-pkgs-*.tar.gz` remains a package-cache archive, not a relocatable environment by itself.
- In the GUI, the primary Python handoff download should be the direct `python-env-*.tar.gz` `conda-pack` archive plus its `.sha256`.
- A secondary Python support bundle may also be offered for convenience, but the enclave delivery artifact is the direct relocatable environment archive, not the cache tarball.

Python environment change policy:

- Treat any requested package addition to an approved Python environment as a new environment request by default.
- Update the source `environment.yml`, rebuild the environment in the build plane, rescan the realized full environment, and deliver a new `conda-pack` bundle.
- Do not treat “add package X later in enclave” as the standard path. A single new package can change transitive dependencies, solver outcomes, and runtime compatibility.
- Wheel-only or overlay-only delivery is allowed only as a narrow exception when all of these are true:
  - the added package is pure Python
  - it does not require upgrades or downgrades of already approved packages
  - it does not introduce native-library or ABI coupling
  - the overlay itself is being governed as a distinct approved add-on
- For Conda-based environments, the default answer is always “rebuild and rescan the full environment,” not “install extra wheels in enclave later.”

R artifacts (per platform/timestamp in evidence bucket):
- Requirements: `evidence/requirements/r/<platform>/<ts>/renv.lock`, `installed-packages.csv`
  - When the scan starts from `requested-packages.json`, that same requirements prefix also includes `requested-packages.json`, and the `renv.lock` there is the generated lockfile from the realized build.
- Model results: `evidence/model-results/r/<platform>/<ts>/osv-report.json`, `trivy-sbom-report.json`
- Governance: `evidence/governance/r/<platform>/<ts>/vulnerability-findings.csv`, `remediation-required.csv`, `remediation-exceptions.csv`, `remediation-spreadsheet.csv`, `governance-summary.json`
- Traceability: `evidence/traceability/r/<platform>/<ts>/run-metadata.json`, `materialization-summary.json`
- Offline deployables: `evidence/packages/offline/r/<platform>/<ts>/renv-cache-*.tar.gz` (+ `.sha256`) and `renv-library-*.tar.gz` (+ `.sha256`)

Current R Linux runtime expectations:
- The active Posit-aligned Linux scanner image is built on an EL8-compatible base, not Amazon Linux 2023.
- The image carries an explicit modern Python at `/usr/local/bin/python3.11`; do not rely on distro-default `python3`.
- Requested-package materialization now runs in a conservative serial mode and clears stale `00LOCK-*` directories before retry/install to avoid retry poisoning from partial installs.

Enclave delivery (Python): pull the Python evidence set above, plus the original `environment.yml` and any offline wheel/conda cache if produced; apply the same approval/transfer flow as R.

### Start R Scan

- Use `scripts/start-r-scan.sh`.
- Ensure `renv.lock` is uploaded first, or pass `--source-lock-file`.
- For ECS-backed scans, the launcher now performs the same prescan capacity
  guard on the target worker ASG. If `MinSize` or `DesiredCapacity` is below
  `1`, the script raises it to `1` before starting the Step Functions run and
  waits for an active ECS container instance.
- You can now also pass `--source-requested-file` with a `requested-packages.json` manifest. In that mode the scan resolves current package versions, materializes them, and emits the generated `renv.lock` as the governed requirement artifact.
- For named approval candidates, upload and run the specific candidate lockfile path instead of relying only on `inputs/r/renv.lock`.
  - Example: `inputs/r/candidates/PI-26.3/linux-amd64/<timestamp>/renv.lock`
- Each successful R workflow now:
  - plans staged restore batches
  - runs sequential CodeBuild stages per platform
  - restores the environment
  - emits materialization evidence
  - publishes both the cache archive and the realized library archive, each with `.sha256`, to `evidence/packages/offline/r/<platform>/<timestamp>/`

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
- If the R worker ASG was reduced to `0` after prior runs, the prescan guard in
  `start-r-scan.sh` now raises it back to at least `1` automatically before the
  next launch.

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
- Offline cache tarball + checksum: `evidence/packages/offline/r/<platform>/<ts>/renv-cache-*.tar.gz` and `.sha256`.
- Offline realized library tarball + checksum: `evidence/packages/offline/r/<platform>/<ts>/renv-library-*.tar.gz` and `.sha256`.
- Requirements snapshot: `installed-packages.csv`.
- Governance outputs: `vulnerability-findings.csv`, `remediation-required.csv`, `remediation-exceptions.csv`, `remediation-spreadsheet.csv`, `governance-summary.json`.
- Model reports: `osv-report.json`, `trivy-sbom-report.json`.
- Run metadata and materialization summary: `run-metadata.json`, `materialization-summary.json`.

Enclave steps (offline):
- Verify both `.sha256` files, place cache, and preserve the realized project library tarball in the transfer set.
- Ensure R 4.4.0 matches scanner; seed the project library from `renv-library-*.tar.gz`, then run `renv::restore()` using the approved `renv.lock`.
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

### Explicit cleanup of failed runs

Use [scripts/cleanup-failed-s3-artifacts.py](../scripts/cleanup-failed-s3-artifacts.py) to remove failed, aborted, or timed-out run artifacts that no longer need to be retained.

What it deletes:
- ephemeral checkpoints under `deploy/tmp/<ecosystem>/checkpoints/...`
- orchestration summaries for failed executions
- failed catalog run records
- matching evidence prefixes for the failed run timestamp/platform when they can be derived safely

Safety rules:
- default mode is dry run
- nothing is deleted until `--write` is passed
- any run whose orchestration summary reports `SUCCEEDED` is skipped

Dry run example for R:

```bash
python3 scripts/cleanup-failed-s3-artifacts.py \
  --ecosystem r \
  --state-machine-arn arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-r-ecs-linux-scan-orchestrator \
  --evidence-bucket package-scanner-dev-scan-evidence-807497180525-us-east-1 \
  --ephemeral-bucket package-scanner-dev-scan-ephemeral-807497180525-us-east-1 \
  --profile AdministratorAccess-807497180525
```

Actually delete:

```bash
python3 scripts/cleanup-failed-s3-artifacts.py \
  --ecosystem r \
  --state-machine-arn arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-r-ecs-linux-scan-orchestrator \
  --evidence-bucket package-scanner-dev-scan-evidence-807497180525-us-east-1 \
  --ephemeral-bucket package-scanner-dev-scan-ephemeral-807497180525-us-east-1 \
  --profile AdministratorAccess-807497180525 \
  --write
```

Target one execution explicitly:

```bash
python3 scripts/cleanup-failed-s3-artifacts.py \
  --ecosystem r \
  --execution-id <execution-id> \
  --evidence-bucket package-scanner-dev-scan-evidence-807497180525-us-east-1 \
  --ephemeral-bucket package-scanner-dev-scan-ephemeral-807497180525-us-east-1 \
  --profile AdministratorAccess-807497180525 \
  --write
```

Bulk cleanup by failed Step Functions status is also supported:

```bash
python3 scripts/cleanup-failed-s3-artifacts.py \
  --ecosystem python \
  --state-machine-arn arn:aws:states:us-east-1:807497180525:stateMachine:package-scanner-dev-python-ecs-linux-scan-orchestrator \
  --evidence-bucket package-scanner-dev-scan-evidence-807497180525-us-east-1 \
  --ephemeral-bucket package-scanner-dev-scan-ephemeral-807497180525-us-east-1 \
  --profile AdministratorAccess-807497180525 \
  --status FAILED \
  --write
```

## Enclave Transfer Checklist

Artifact locations (R linux):
- Evidence bucket: `s3://package-scanner-dev-scan-evidence-<acct>-<region>`
- Offline deployables:
  - `evidence/packages/offline/r/linux-amd64/<timestamp>/renv-cache-linux-amd64-<timestamp>.tar.gz` (+ `.sha256`)
  - `evidence/packages/offline/r/linux-amd64/<timestamp>/renv-library-linux-amd64-<timestamp>.tar.gz` (+ `.sha256`)
- Lockfile: `evidence/requirements/r/linux-amd64/<timestamp>/renv.lock`
- Governance/model: `evidence/governance/r/linux-amd64/<timestamp>/...` (findings/remediation CSVs, governance-summary.json), `evidence/model-results/r/linux-amd64/<timestamp>/osv-report.json`, `trivy-sbom-report.json`
- Traceability: `evidence/traceability/r/linux-amd64/<timestamp>/run-metadata.json`, `materialization-summary.json`, `installed-packages.csv`

Transfer steps (airgapped R):
1. Pull the lockfile, cache tarball, library tarball, their `.sha256` files, and the governance/model/traceability bundle from the evidence bucket.
2. Deliver the artifact bundle to cyber and obtain approval before any enclave install.
3. After approval, move the approved artifacts via the sanctioned transfer path.
4. In the enclave (per platform):
   - Linux cache path example: `/opt/renv/cache`; Windows cache path example: `C:\renv\cache`.
   - Create the cache root first. Example: Linux `mkdir -p /opt/renv/cache`; Windows `New-Item -ItemType Directory -Force C:\renv\cache`.
   - Extract the cache archive into that cache root, not into `/`. The cache archive contains cache contents only.
   - Untar cache: Linux `tar -xzf renv-cache-linux-amd64-<timestamp>.tar.gz -C /opt/renv/cache`; Windows use 7zip/PowerShell to extract into `C:\renv\cache`.
   - Preserve `renv-library-*.tar.gz` with the deployment bundle. For Posit/Workbench-style restores, seed the project library from that archive before running `renv::restore()`.
   - Set cache env: Linux `export RENV_PATHS_CACHE=/opt/renv/cache`; Windows `set RENV_PATHS_CACHE=C:\renv\cache`.
   - Ensure R 4.4.0 is installed and on PATH.
   - Ensure the `renv` package is already installed on the enclave host image. The cache bundle restores the project library; it is not a bootstrap installer for `renv` itself.
   - Do not point `repos` at `https://cloud.r-project.org` or Posit Package Manager inside the enclave. The restore validation must be no-network and use only the transferred deployables.
   - Restore:
     - Linux:
       ```bash
       R -q <<'RSCRIPT'
       options(repos = c(CRAN = "file:///nonexistent-cran", RSPM = "file:///nonexistent-rspm"))
       Sys.setenv(
         RENV_PATHS_CACHE = "/opt/renv/cache",
         RENV_CONFIG_CACHE_SYMLINKS = "FALSE"
       )
       stopifnot(requireNamespace("renv", quietly = TRUE))
       renv::consent(provided = TRUE)
       renv::restore(lockfile = "renv.lock", prompt = FALSE, clean = TRUE)
       RSCRIPT
       ```
     - Windows (PowerShell):
       ```powershell
       $env:RENV_PATHS_CACHE="C:\renv\cache"
       $env:RENV_CONFIG_CACHE_SYMLINKS="FALSE"
       Rscript -e "options(repos=c(CRAN='file:///nonexistent-cran',RSPM='file:///nonexistent-rspm')); if(!requireNamespace('renv',quietly=TRUE)) stop('renv package must be preinstalled on enclave host'); renv::consent(provided=TRUE); renv::restore(lockfile='renv.lock', prompt=FALSE, clean=TRUE)"
       ```
   - Confirm library path from `renv/library-path.txt`; default is `~/.local/share/renv/library` (Linux) or `%USERPROFILE%\\AppData\\Local\\renv\\library` (Windows).
   - Validate the restore result against the approved evidence bundle:
     - compare package count against `materialization-summary.json` `counts.restored_packages`
     - compare package/version inventory against `installed-packages.csv`
     - retain the enclave-side restore log with the transferred evidence set

Air-gap restore validation steps:
1. Before transfer, verify both tarball checksums:
   - `sha256sum -c renv-cache-linux-amd64-<timestamp>.tar.gz.sha256`
   - `sha256sum -c renv-library-linux-amd64-<timestamp>.tar.gz.sha256`
2. After extraction in the enclave, confirm the cache is populated before any restore:
   - Linux: `find /opt/renv/cache -maxdepth 3 -type d | head`
   - Windows: `Get-ChildItem C:\renv\cache -Depth 3 | Select-Object -First 20`
3. Run the no-network restore command above with repos set to nonexistent `file:///` URLs. This is the explicit enclave restore test.
4. Export the realized package inventory and compare it with the approved evidence:
   - Linux:
     ```bash
     Rscript -e "write.csv(as.data.frame(installed.packages()[,c('Package','Version')]), 'enclave-installed-packages.csv', row.names=FALSE)"
     ```
   - Windows:
     ```powershell
     Rscript -e "write.csv(as.data.frame(installed.packages()[,c('Package','Version')]), 'enclave-installed-packages.csv', row.names=FALSE)"
     ```
5. Accept the restore only if:
   - the restore completed without any package download attempts
   - `enclave-installed-packages.csv` matches the approved package/version rows from `installed-packages.csv`
   - package count is consistent with `materialization-summary.json`
