# Posit Handoff Runbook

Use this runbook when the approved R offline bundle from `package_scanner` must be restored onto a Linux Posit-hosted environment.

This document intentionally separates Posit Workbench from Posit Connect:

- Posit Workbench: supported target for manual offline `renv` restore from the approved deployable bundle (`renv-cache` plus `renv-library`).
- Posit Connect: do not manually unpack the tarball into Connect-managed runtime cache paths. Connect owns those caches and rebuilds them as part of content deployment.

## 1. Preferred Linux Location

For a Linux Posit Workbench deployment, use a shared admin-managed cache root:

- shared cache root: `/opt/posit/renv/cache/R-4.4.0`
- project/app root example: `/opt/posit/projects/<application-name>`

Why this location:

- it keeps the approved cache outside user home directories
- it is stable across Workbench sessions
- it is easy to mount, back up, and permission separately from user content
- it avoids colliding with Posit Connect internal runtime storage under `/var/lib/rstudio-connect`

Do not extract the bundle into `/`, `/home/<user>`, or `/var/lib/rstudio-connect`.

## 2. Required Handoff Bundle

From the approved scan run, transfer:

- `renv.lock`
- `renv-cache-linux-amd64-<timestamp>.tar.gz`
- `renv-cache-linux-amd64-<timestamp>.tar.gz.sha256`
- `renv-library-linux-amd64-<timestamp>.tar.gz`
- `renv-library-linux-amd64-<timestamp>.tar.gz.sha256`
- `installed-packages.csv`
- `materialization-summary.json`
- `run-metadata.json`

## 3. Transfer To The Posit Host

Use a temporary staging directory on the Posit server for the transferred
bundle, separate from the final Posit project/cache locations.

Recommended locations:

- transferred bundle staging: `/var/tmp/pi26.3-handoff`
- verifier work/log directory: `/var/tmp/pi26.3-verify`

Example:

```bash
sudo rm -rf /var/tmp/pi26.3-handoff /var/tmp/pi26.3-verify
sudo mkdir -p /var/tmp/pi26.3-handoff /var/tmp/pi26.3-verify
sudo chown -R <your-user>:<your-group> /var/tmp/pi26.3-handoff /var/tmp/pi26.3-verify
```

Copy the downloaded `Posit handoff bundle` ZIP to the Posit host and place it
under `/var/tmp/pi26.3-handoff`, then unzip it there:

```bash
cd /var/tmp/pi26.3-handoff
unzip /path/to/r-scan-<execution>-linux-amd64-posit-handoff-bundle.zip
chmod +x scripts/verify-posit-handoff*
```

After unzip, `/var/tmp/pi26.3-handoff` should contain:

- `renv.lock`
- `installed-packages.csv`
- `materialization-summary.json`
- `run-metadata.json`
- `renv-cache-linux-amd64-<timestamp>.tar.gz`
- `renv-cache-linux-amd64-<timestamp>.tar.gz.sha256`
- `renv-library-linux-amd64-<timestamp>.tar.gz`
- `renv-library-linux-amd64-<timestamp>.tar.gz.sha256`
- `scripts/verify-posit-handoff.sh`
- `scripts/verify-posit-handoff-inside.sh`

## 3. Posit Workbench Restore Procedure

### Prepare directories

```bash
sudo mkdir -p /opt/posit/renv/cache/R-4.4.0
sudo mkdir -p /opt/posit/projects/<application-name>
sudo chown -R <project-owner>:<project-group> /opt/posit/projects/<application-name>
```

If multiple Workbench users need to reuse the cache, grant read and execute access on `/opt/posit/renv/cache/R-4.4.0` to the group that owns the project content.

### Verify and extract the bundle

```bash
cd /path/to/transferred/bundle
sha256sum -c renv-cache-linux-amd64-<timestamp>.tar.gz.sha256
sha256sum -c renv-library-linux-amd64-<timestamp>.tar.gz.sha256
sudo tar -xzf renv-cache-linux-amd64-<timestamp>.tar.gz -C /opt/posit/renv/cache/R-4.4.0
sudo mkdir -p /opt/posit/projects/<application-name>/renv/library/linux-rhel-8.10/R-4.4/x86_64-pc-linux-gnu
sudo tar -xzf renv-library-linux-amd64-<timestamp>.tar.gz \
  -C /opt/posit/projects/<application-name>/renv/library/linux-rhel-8.10/R-4.4/x86_64-pc-linux-gnu
```

The cache tarball contains cache contents only. Extract into the cache root, not into `/`.

The realized library tarball is the deployment-side bootstrap for air-gapped
restore and must be preserved in the transferred handoff bundle. The automated
verifier seeds the project library from it before running `renv::restore()`.

If the Posit host uses a different `renv` platform path than `linux-rhel-8.10`,
determine it first and adjust the extraction target:

```bash
Rscript --vanilla -e "cat(renv::paths\$library(project='/opt/posit/projects/<application-name>'), '\n')"
```

The realized library archive must be extracted into that exact project-library
path, not just into the project root.

### Stage the project

```bash
cp renv.lock /opt/posit/projects/<application-name>/
cp installed-packages.csv /opt/posit/projects/<application-name>/
cp materialization-summary.json /opt/posit/projects/<application-name>/
```

Create `/opt/posit/projects/<application-name>/.Renviron` with:

```bash
RENV_PATHS_CACHE=/opt/posit/renv/cache/R-4.4.0
RENV_CONFIG_CACHE_SYMLINKS=FALSE
```

If this Posit server is dedicated to this workflow, you can set the same variables globally instead of per project.

### Restore offline

Prerequisites:

- R 4.4.0 is installed on the Posit host
- the `renv` package is already installed into that R installation
- if the restored package set includes Java-backed packages such as `rJava`,
  `DatabaseConnector`, or `FeatureExtraction`, a JDK/JRE providing `libjvm.so`
  must be installed on the Posit host
- the restore is run from the project directory as the same Linux user who will own the project library

Run:

```bash
cd /opt/posit/projects/<application-name>
Rscript --vanilla -e "options(repos=c(CRAN='file:///nonexistent-cran',RSPM='file:///nonexistent-rspm')); Sys.setenv(RENV_PATHS_CACHE='/opt/posit/renv/cache/R-4.4.0', RENV_CONFIG_CACHE_SYMLINKS='FALSE'); stopifnot(requireNamespace('renv', quietly=TRUE)); renv::consent(provided=TRUE); project <- normalizePath('.', mustWork=TRUE); library <- renv::paths\$library(project=project); .libPaths(unique(c(library, .libPaths()))); renv::restore(project=project, library=library, lockfile='renv.lock', prompt=FALSE, clean=TRUE)"
```

This is the no-network validation. The project library should already be seeded
from `renv-library-*.tar.gz` before this command runs. `renv::restore()` is
then reconciling the seeded project library against the approved lockfile, not
bootstrapping from an empty library.

If the restore tries to reach CRAN or Posit Package Manager, treat that as a
failed air-gap restore.

### Java-backed package note

Some restored packages require a JVM at runtime even after the offline restore
itself succeeds. In this workflow, that most commonly appears through:

- `rJava`
- `DatabaseConnector`
- `FeatureExtraction`

If package load fails with `libjvm.so: cannot open shared object file`, set the
runtime environment before launching `Rscript`:

```bash
LIBJVM_PATH="$(find /usr/lib/jvm /usr/java -name libjvm.so 2>/dev/null | head -n 1)"
export JAVA_HOME="$(dirname "$(dirname "$LIBJVM_PATH")")"
export LD_LIBRARY_PATH="$(dirname "$LIBJVM_PATH"):${LD_LIBRARY_PATH:-}"
export R_LIBS_USER=/opt/posit/projects/<application-name>/renv/library/linux-rhel-8.10/R-4.4/x86_64-pc-linux-gnu
export RENV_PATHS_CACHE=/opt/posit/renv/cache/R-4.4.0
export RENV_CONFIG_CACHE_SYMLINKS=FALSE
```

Then test:

```bash
cd /opt/posit/projects/<application-name>
Rscript --vanilla -e "library(rJava); .jinit(); cat('rJava ok\n')"
Rscript --vanilla -e "library(FeatureExtraction); cat('FeatureExtraction ok\n')"
```

## 4. Validation Steps

After restore:

1. Confirm the cache is populated:
   - `find /opt/posit/renv/cache/R-4.4.0 -maxdepth 3 -type d | head`
2. Confirm the seeded project library exists:
   - `Rscript --vanilla -e "project <- '/opt/posit/projects/<application-name>'; cat(renv::paths\$library(project=project), '\n')"`
   - `find /opt/posit/projects/<application-name>/renv/library -maxdepth 4 -type d | head`
2. Export the realized package inventory:
   - `Rscript -e "write.csv(as.data.frame(installed.packages()[,c('Package','Version')]), 'enclave-installed-packages.csv', row.names=FALSE)"`
3. Compare the result to the approved evidence:
   - package count should align with `materialization-summary.json` `counts.restored_packages`
   - package names and versions should align with `installed-packages.csv`
4. Open the project in Posit Workbench and run:
   - `renv::status()`
5. Accept the restore only if:
   - the restore completed without download attempts
   - the installed package inventory matches the approved run evidence
   - key runtime packages load successfully on the Posit host

For Bioconductor-aware air-gap restores, treat `verification-summary.txt` and
successful package loads as the primary acceptance signal. `renv::status()` can
still attempt Bioconductor metadata/bootstrap work in ways that are not a good
offline verification signal.

### Automated verifier

You can run the same handoff procedure automatically against the exact target
Linux image before transferring anything into the enclave.

Example:

```bash
./scripts/verify-posit-handoff.sh \
  --image <exact-posit-r44-image> \
  --evidence-bucket <evidence-bucket> \
  --timestamp <approved-run-timestamp> \
  --profile <aws-profile> \
  --app-name <application-name>
```

Directly on a Posit server after the handoff bundle has been transferred:

```bash
cd /var/tmp/pi26.3-handoff

./scripts/verify-posit-handoff.sh \
  --local-host \
  --bundle-dir /var/tmp/pi26.3-handoff \
  --app-name pi26.3_full \
  --work-dir /var/tmp/pi26.3-verify
```

What it does:

- downloads the approved handoff bundle from S3, including `renv-cache` and `renv-library`
- starts the supplied image with `--network none`
- stages the bundle into `/opt/posit/renv/cache/R-4.4.0` and `/opt/posit/projects/<application-name>`
- seeds the project library from the realized `renv-library-*.tar.gz` artifact
- runs the no-network `renv::restore()` command from this runbook after seeding the project library
- exports `enclave-installed-packages.csv`, `renv-status.txt`, `restore.log`, and a verification summary
- fails if any approved package/version is missing from the realized inventory

With `--local-host`, the same script skips Docker and runs the restore directly
on the current Linux host. Use that mode on the Posit server itself when you
want a single command instead of a manual runbook procedure.

After the verifier completes, inspect:

```bash
find /var/tmp/pi26.3-verify/results -maxdepth 1 -type f | sort
cat /var/tmp/pi26.3-verify/results/verification-summary.txt
tail -n 80 /var/tmp/pi26.3-verify/results/restore.log
cat /var/tmp/pi26.3-verify/results/renv-status.txt
```

Use this verifier when you need proof that the enclave restore will work on the
exact Posit-aligned runtime image, not just on the generic scanner image.

## 5. Posit Connect Boundary

Do not manually place these offline tarballs under `/var/lib/rstudio-connect` or other Posit Connect managed runtime-cache directories.

Reason:

- Posit Connect manages runtime caches and rebuilds them during content deployment
- hand-injecting files into Connect-managed cache/state paths is operationally brittle and not the right deployment boundary

If the target platform is Posit Connect, use one of these approaches instead:

- restore and validate the project on a Posit Workbench or equivalent Linux staging host first, then publish the validated content through the normal Connect workflow
- provide Connect access to an internal package source such as an approved Posit Package Manager repository or equivalent internal CRAN-like mirror

For this project, the offline bundle should be treated as a Workbench or staging-host restore artifact, not a direct Connect cache import.
