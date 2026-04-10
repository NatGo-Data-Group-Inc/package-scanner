# Posit Handoff Runbook

Use this runbook when the approved R offline bundle from `package_scanner` must be restored onto a Linux Posit-hosted environment.

This document intentionally separates Posit Workbench from Posit Connect:

- Posit Workbench: supported target for manual offline `renv` restore from the approved cache tarball.
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
- `installed-packages.csv`
- `materialization-summary.json`
- `run-metadata.json`

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
sudo tar -xzf renv-cache-linux-amd64-<timestamp>.tar.gz -C /opt/posit/renv/cache/R-4.4.0
```

The tarball contains cache contents only. Extract into the cache root, not into `/`.

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
- the restore is run from the project directory as the same Linux user who will own the project library

Run:

```bash
cd /opt/posit/projects/<application-name>
Rscript -e "options(repos=c(CRAN='file:///nonexistent-cran',RSPM='file:///nonexistent-rspm')); stopifnot(requireNamespace('renv', quietly=TRUE)); renv::consent(provided=TRUE); renv::restore(lockfile='renv.lock', prompt=FALSE, clean=TRUE)"
```

This is the no-network validation. If the restore tries to reach CRAN or Posit Package Manager, treat that as a failed air-gap restore.

## 4. Validation Steps

After restore:

1. Confirm the cache is populated:
   - `find /opt/posit/renv/cache/R-4.4.0 -maxdepth 3 -type d | head`
2. Export the realized package inventory:
   - `Rscript -e "write.csv(as.data.frame(installed.packages()[,c('Package','Version')]), 'enclave-installed-packages.csv', row.names=FALSE)"`
3. Compare the result to the approved evidence:
   - package count should align with `materialization-summary.json` `counts.restored_packages`
   - package names and versions should align with `installed-packages.csv`
4. Open the project in Posit Workbench and run:
   - `renv::status()`
5. Accept the restore only if:
   - the restore completed without download attempts
   - `renv::status()` reports the project is synchronized
   - the installed package inventory matches the approved run evidence

## 5. Posit Connect Boundary

Do not manually place this cache tarball under `/var/lib/rstudio-connect` or other Posit Connect managed runtime-cache directories.

Reason:

- Posit Connect manages runtime caches and rebuilds them during content deployment
- hand-injecting files into Connect-managed cache/state paths is operationally brittle and not the right deployment boundary

If the target platform is Posit Connect, use one of these approaches instead:

- restore and validate the project on a Posit Workbench or equivalent Linux staging host first, then publish the validated content through the normal Connect workflow
- provide Connect access to an internal package source such as an approved Posit Package Manager repository or equivalent internal CRAN-like mirror

For this project, the offline bundle should be treated as a Workbench or staging-host restore artifact, not a direct Connect cache import.
