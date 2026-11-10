# AGENTS.md

Context for AI coding agents working in this repository. Claude Code, Codex and
any other agent should read this first. It is the shared memory between
sessions and between tools.

Keep this file current. If you learn something here that is wrong, fix it in
the same change as the work that proved it wrong.

If `AGENTS.local.md` exists in this directory, read it as well. It holds
operator-specific context and is not committed.

---

## What this project is

An AWS package-security scanner for Python and R environments. It takes a
description of an environment, works out exactly which packages that implies,
scans them for known vulnerabilities, and produces the evidence Cyber needs to
approve the environment for use in the MIP.

It runs in the NatGo dev AWS account, not in the MIP itself.

## Current deployment

| | |
|---|---|
| Account / region | `807497180525` / `us-east-1` |
| Profile | `AdministratorAccess-807497180525` |
| Python ECS stack | `cyber-scanner-dev-python-ecs` |
| Webapp stack | `package-scanner-webapp-dev` |
| Input bucket | `package-scanner-dev-scan-input-807497180525-us-east-1` |
| Evidence bucket | `package-scanner-dev-scan-evidence-807497180525-us-east-1` |
| Ephemeral bucket | `package-scanner-dev-scan-ephemeral-807497180525-us-east-1` |
| Linux worker image | Rocky Linux 8, glibc 2.28, utility Python 3.11 |
| Task family | `package-scanner-dev-python-linux-amd64` |

Always verify the live image digest and task-definition revision before
trusting these. A rebuilt image is not active until the running ECS task shows
the new revision.

---

## The three Python input modes

Choosing the wrong one is the most common and most expensive mistake in this
repository. They produce different artifacts from the same packages.

**`environment-yaml` (Blueprint).** A conda `environment.yml`. Version pins act
as constraints, and the solver resolves the full environment around them. The
realized result can differ in transitive detail from what you asked for. Runs a
conda preflight dry-solve before anything is downloaded.

**`requested-packages`.** An unversioned package list. The scanner resolves
each name to whatever is current and always materializes temporarily before the
final scan. Convenient and dangerous: what gets approved is not what you
submitted. Never use this for anything going to Cyber unless the point is
genuinely "give me current versions."

**`requirements-lock` (Locked).** A pinned `package==version` pip list. Scanned
exactly as supplied.

### The one combination that skips the conda solve

```
--input-type requirements-lock  +  --materialize-after-scan false
```

This is the only path that does not run the conda preflight. From
`scripts/run-python-ecs-task.sh`:

```bash
# Package-validation gate before package archives are downloaded. This is deliberately skipped
# only for an analysis-only pip requirements scan, which has no Conda plan.
if [[ "${REQUIREMENTS_ONLY_SCAN}" != "true" || "${MATERIALIZE_AFTER_SCAN}" == "true" ]]; then
```

Use it to scan an environment that already exists and does not need rebuilding.
Note that `render_requirements_environment` hardcodes `python=3.13` in the
environment it generates on this path; it is not used for solving, but it is a
fidelity wrinkle worth disclosing in anything handed to Cyber.

---

## Capturing an environment that already exists

Do **not** hand-build a candidate from `conda-meta` filenames or a plain
`conda env export`. Use the script built for it:

```bash
python3 scripts/build-candidate-from-env-artifacts.py \
  --name <candidate-name> \
  --env-prefix <conda-env-prefix> \
  --conda-bin <path-to-conda> \
  --prefer-conda-available \
  --target-subdir linux-64 \
  --output candidates/<candidate-name>.yml \
  --artifacts-dir artifacts/<candidate-name> \
  --artifacts-zip artifacts/<candidate-name>.zip
```

`--prefer-conda-available` probes the configured channels and keeps a package on
the conda side only when conda can actually satisfy it, falling back to pip
otherwise. Skipping this probe is how you get an unsatisfiable solve from an
environment that demonstrably works on disk.

`--target-subdir linux-64` matters whenever you capture on a platform other
than the scan target.

**Do not pin exact conda build strings by hand.** Prefer `idna=3.15` over
`idna=3.15=<build>`. A build string that no longer exists in the channel makes
the solve unsatisfiable even though the environment runs fine. See
`docs/troubleshooting.md`, "Python candidate pin exists by version but not by
exact build."

---

## Running a scan

```bash
export AWS_PROFILE=AdministratorAccess-807497180525

./scripts/start-python-scan.sh \
  --stack-name cyber-scanner-dev-python-ecs \
  --input-bucket package-scanner-dev-scan-input-807497180525-us-east-1 \
  --source-environment-file ./<file>.yml \
  --input-type environment-yaml \
  --materialize-after-scan true \
  --platform-set linux-amd64 \
  --region us-east-1 \
  --profile AdministratorAccess-807497180525 \
  --expected-account-id 807497180525 \
  --deployment-lock-token dev-lock-20260306 \
  --remediate-medium true \
  --fail-on-medium false
```

Gotchas that each cost someone an afternoon:

- `--platform-set linux-only` fails. The arm64 ASG has max capacity 0, so use
  `linux-amd64`.
- The CLI uploads to a **fixed key**, `inputs/python/environment.yml`, so your
  filename is lost and two concurrent scans overwrite each other. The per-run
  copy survives at `requirements/<run>/environment.original.yml`. Per-candidate
  keys under `inputs/python/candidates/<name>/` exist for the GUI path.
- The lock token is a stack tag, currently `dev-lock-20260306`.

---

## Reading a failure

**Do not trust the run status.** Check the log, not the clock and not the
status field.

```bash
STREAM=$(aws logs describe-log-streams \
  --log-group-name /ecs/package-scanner-dev/python/linux-amd64 \
  --order-by LastEventTime --descending --limit 1 \
  --region us-east-1 --query "logStreams[0].logStreamName" --output text)

aws logs get-log-events \
  --log-group-name /ecs/package-scanner-dev/python/linux-amd64 \
  --log-stream-name "$STREAM" --region us-east-1 \
  --limit 60 --query "events[].message" --output text | tail -5
```

Use `--limit`, not `--max-items`; the latter appends a pagination token that
corrupts the variable.

What the messages mean:

| Log line | Meaning |
|---|---|
| `Conda dry solve failed` | Genuine unsatisfiable solve. The input is the problem. |
| `requirements.lock.txt` not found | The solve succeeded. Artifact of a conda-only input in scan-only mode. |
| `Materialization validation failed with exit 4` | Usually the `conda_package_count` defect. Artifacts are present and valid. |

The container logs a one-line summary and discards the solver's actual conflict
output, so a failed solve cannot be diagnosed from the log alone.

---

## Before handing anything to Cyber

Run the six checks in `docs/verifying-a-python-artifact.md`. They take about
five minutes and they exist because an artifact has reached Cyber built on the
wrong interpreter with every pin discarded, while reporting success.

The first check is the important one: read back the `environment.yml` the
scanner actually consumed and confirm it is yours. Signs it is not:

- `name: target` with no interpreter pin of yours
- `python=3.13` when you asked for something else
- package names with no `==` version attached

---

## Known defects

Verify before relying on any of these; they are as of commit `cd27b79`.

1. **Nothing verifies the environment works on the target.** The pipeline proves
   packages install in the build container and carry no blocking findings. It
   does not prove they import on the host that will run them. A bundle has
   scanned clean and then failed to load on the target because of a glibc
   mismatch.
2. **`conda_package_count: 0` fails good runs.** `load_conda_packages` in
   `scripts/generate-python-materialization-summary.py` returns 0 when the JSON
   is not a list, and the validator then reports requested packages missing.
   Good runs report FAILED with every artifact present.
3. **`packages/offline/` is not the offline bundle.** It is the micromamba conda
   cache. The installable environment is the tarball under `env-artifacts/`.
4. **Container logs truncate on failure**, discarding the detail that would
   explain it.
5. **Fixed input key** prevents concurrent CLI scans, as above.

---

## Rules for agents working here

- Never commit to `main`. Branch, push, open a pull request.
- `feature/conda-preflight-aws` is the current baseline and is ahead of `main`;
  check by how much with
  `git rev-list --count origin/main..origin/feature/conda-preflight-aws`.
  Treat it as trunk until that is reconciled.
- **This repository is public.** No credentials, no tokens, no personal
  information, no internal correspondence.
- Never `git add .`. Add named files. Scratch output, downloaded evidence and
  extracted archives have ended up in this folder before.
- Do not loosen pins or drop packages to make a scan pass. A green scan of an
  environment nobody runs is worse than a red one.
- When a run fails, read the log before forming a theory, and verify the theory
  before acting on it.

---

## Where to read more

| Document | Covers |
|---|---|
| `docs/handoff-runbook.md` | Operator guide. Written for the CodeBuild path; the Python ECS path differs. |
| `docs/operations-runbook.md` | Current Python ECS procedures, candidate capture, image refresh. Most current of the runbooks. |
| `docs/troubleshooting.md` | Failure modes and exact remediations. Read before debugging anything. |
| `docs/workflow.md` | End-to-end worked examples for Python and R. |
| `docs/cli-switch-reference.md` | Every switch on every script. |
| `docs/cyber-artifacts.md` | File-by-file reference for the Cyber review bundle. |
| `docs/python-preflight-failfast.md` | Why a failed preflight stops the run. Deliberate, since 16 Sep 2026. |
| `docs/lead-handoff-prompt.md` | Tom Jansto's handoff context. |
| `PIPELINE-STATE.local.md` | Current state and the status of every known issue. **Local only, not committed.** Ask the operator if it is missing. |
| `docs/verifying-a-python-artifact.md` | The six checks to run before handing an artifact to Cyber. |
