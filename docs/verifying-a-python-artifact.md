# Verifying a Python scan artifact

**Before handing anything to Cyber, verify it. Do not trust the run status.**

This procedure exists because an artifact has reached Cyber built on the wrong
interpreter with every version pin discarded.
The run reported success. Nothing in the output said anything was wrong. It was
caught only by diffing the input the scanner actually consumed against the file
that was submitted, which is check 1 below.

Run all six. They take about five minutes.

---

## Setup

```bash
B=s3://package-scanner-dev-scan-evidence-807497180525-us-east-1/evidence
TS=<timestamp from the run name>
RUN=<full run name>
R=python/linux-amd64/$TS/$RUN
P="--profile AdministratorAccess-807497180525"
```

---

## 1. Did the scanner use the file you submitted?

Do this first. It is the check that catches the worst class of error.

```bash
aws s3 cp $B/requirements/$R/environment.yml - $P | head -30
```

The interpreter pin should be the one you specified, and **every package should
carry `==`**.

Signs that requested-package-list mode ran instead of blueprint mode:

- `name: target` with no interpreter pin of yours
- `python=3.13` when you asked for something else
- package names with no version attached

Any of those means what was scanned is not what you submitted. Stop and rebuild.

## 2. Which interpreter and which wheels?

```bash
aws s3 cp $B/env-artifacts/$R/restore.log - $P \
  | grep -oE "python==?3\.[0-9]+\.[0-9]+|manylinux_2_[0-9]+" | sort -u
```

The interpreter must match what you asked for.

**Nothing above `manylinux_2_28`.** The MIP target is glibc 2.28. A
`manylinux_2_34` wheel installs fine in a newer build container and then fails
to load on the target. A bundle has scanned clean and then failed to load on the
target this way.

## 3. Did anything resolve differently from what you asked for?

```bash
aws s3 cp $B/requirements/$R/requirements.lock.txt - $P > /tmp/lock.txt
grep -oE '^\s+- [A-Za-z0-9_.-]+==.*' <your-file>.yml \
  | sed 's/^[[:space:]]*- //' | tr 'A-Z_' 'a-z-' | sort > /tmp/want.txt
tr 'A-Z_' 'a-z-' < /tmp/lock.txt | sort > /tmp/got.txt
comm -23 /tmp/want.txt /tmp/got.txt
```

Empty is good. Anything listed resolved to a version other than the one you
pinned and must be disclosed.

The pipeline does not warn about this. In one run `cuda-pathfinder` resolved
to 1.8.4 against an explicit `==1.8.3` with no mention anywhere in the output.

## 4. Did everything install?

```bash
aws s3 cp $B/traceability/$R/package-validation-installed-inventory-difference.json - $P
```

Both arrays must be empty.

## 5. What will Cyber see?

```bash
aws s3 cp $B/governance/$R/remediation-required.csv - $P
```

HIGH and CRITICAL block approval. MEDIUM is reported but allowed. LOW is
informational.

## 6. Is the bundle the right bundle?

**The archive under `packages/offline/` is not the offline package bundle.** It
is the micromamba conda cache. In one real run it was 119 MB with no pip
packages, while the real packages lived only in the multi-GB environment
tarball under `env-artifacts/`.

Anyone shipping the file whose path says `packages/offline` to an air-gapped
target delivers an environment with three packages in it.

```bash
aws s3 ls $B/env-artifacts/$R/ $P
```

Confirm the `python-env-*.tar.gz` is present and is the size you expect, and
that its `.sha256` is alongside it.

---

## What goes to Cyber

| Path | What it is |
|---|---|
| `env-artifacts/.../python-env-*.tar.gz` | **The installable environment. This is the deliverable.** |
| `requirements/.../requirements.lock.txt` | Resolved inventory, the list being approved |
| `requirements/.../environment.original.yml` | The file exactly as submitted |
| `governance/.../remediation-required.csv` | Blocking findings |
| `governance/.../remediation-exceptions.csv` | Documented exceptions |
| `governance/.../vulnerability-findings.csv` | Everything, including LOW |
| `env-artifacts/.../python-packages.cdx.json` | SBOM |
| `model-results/.../` | Trivy, OSV, safety |

Not the file under `packages/offline/`.

---

## Reading run timing

Rough guide only. Always confirm against the log; timing has misled us before.

| Elapsed | Usually means |
|---|---|
| Fails under 60s | Never got past plan validation. Config or input problem. |
| Fails 3 to 6 min | Installed, then failed, or a source build died. |
| Still RUNNING after 2 min | Installing. Leave it. |

A full materialize of 300-odd packages with torch takes 10 to 35 minutes.

---

## When a run fails

Container logs truncate when the task dies, so the evidence bucket is more
reliable than CloudWatch for anything that got past validation:

```bash
aws s3 cp $B/traceability/$R/materialization-validation-error.txt - $P
aws s3 cp $B/env-artifacts/$R/restore.log - $P | tail -60
```

The second names the package if a source build failed.

For failures before materialization, nothing is written to S3 at all and the
container log is the only record. See `AGENTS.md`, "Reading a failure."
