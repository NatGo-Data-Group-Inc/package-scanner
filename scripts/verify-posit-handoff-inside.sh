#!/usr/bin/env bash
set -euo pipefail

BUNDLE_DIR=""
OUTPUT_DIR=""
APP_NAME="posit-restore-verification"
CACHE_ROOT="/opt/posit/renv/cache/R-4.4.0"
PROJECTS_ROOT="/opt/posit/projects"

usage() {
  cat <<'EOF'
Usage: verify-posit-handoff-inside.sh --bundle-dir <dir> --output-dir <dir> [options]

Run the Posit handoff restore procedure inside a target Linux image and fail if
the approved package inventory cannot be restored offline.

Options:
  --bundle-dir <dir>      Directory containing renv.lock, installed-packages.csv,
                          materialization-summary.json, run-metadata.json,
                          renv-cache-*.tar.gz, and matching .sha256
  --output-dir <dir>      Directory to write verification outputs
  --app-name <name>       Project directory name under /opt/posit/projects
  --cache-root <path>     Cache extraction root (default: /opt/posit/renv/cache/R-4.4.0)
  --projects-root <path>  Parent directory for project root (default: /opt/posit/projects)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle-dir) BUNDLE_DIR="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --app-name) APP_NAME="$2"; shift 2 ;;
    --cache-root) CACHE_ROOT="$2"; shift 2 ;;
    --projects-root) PROJECTS_ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${BUNDLE_DIR}" || -z "${OUTPUT_DIR}" ]]; then
  echo "--bundle-dir and --output-dir are required." >&2
  usage >&2
  exit 1
fi

required_cmds=(bash cp grep mkdir mv printf rm sed sha256sum sort tar tee)
for cmd in "${required_cmds[@]}"; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Required command not found in target image: ${cmd}" >&2
    exit 1
  fi
done

if ! command -v Rscript >/dev/null 2>&1; then
  echo "Rscript not found in target image." >&2
  exit 1
fi

mkdir -p "${OUTPUT_DIR}"
LOG_PATH="${OUTPUT_DIR}/posit-restore-verification.log"
exec > >(tee "${LOG_PATH}") 2>&1

LOCK_FILE="${BUNDLE_DIR}/renv.lock"
INSTALLED_CSV="${BUNDLE_DIR}/installed-packages.csv"
SUMMARY_JSON="${BUNDLE_DIR}/materialization-summary.json"
RUN_METADATA_JSON="${BUNDLE_DIR}/run-metadata.json"
CACHE_ARCHIVE="$(find "${BUNDLE_DIR}" -maxdepth 1 -type f -name 'renv-cache-*.tar.gz' | sort | head -n1)"
CACHE_CHECKSUM="$(find "${BUNDLE_DIR}" -maxdepth 1 -type f -name 'renv-cache-*.tar.gz.sha256' | sort | head -n1)"
LIBRARY_ARCHIVE="$(find "${BUNDLE_DIR}" -maxdepth 1 -type f -name 'renv-library-*.tar.gz' | sort | head -n1)"
LIBRARY_CHECKSUM="$(find "${BUNDLE_DIR}" -maxdepth 1 -type f -name 'renv-library-*.tar.gz.sha256' | sort | head -n1)"

for required_file in "${LOCK_FILE}" "${INSTALLED_CSV}" "${SUMMARY_JSON}" "${RUN_METADATA_JSON}"; do
  if [[ ! -f "${required_file}" ]]; then
    echo "Missing required bundle artifact: ${required_file}" >&2
    exit 1
  fi
done

if [[ -z "${CACHE_ARCHIVE}" || ! -f "${CACHE_ARCHIVE}" ]]; then
  echo "Could not find renv cache archive in ${BUNDLE_DIR}" >&2
  exit 1
fi
if [[ -z "${CACHE_CHECKSUM}" || ! -f "${CACHE_CHECKSUM}" ]]; then
  echo "Could not find renv cache checksum in ${BUNDLE_DIR}" >&2
  exit 1
fi
if [[ -z "${LIBRARY_ARCHIVE}" || ! -f "${LIBRARY_ARCHIVE}" ]]; then
  echo "Could not find renv library archive in ${BUNDLE_DIR}" >&2
  exit 1
fi
if [[ -z "${LIBRARY_CHECKSUM}" || ! -f "${LIBRARY_CHECKSUM}" ]]; then
  echo "Could not find renv library checksum in ${BUNDLE_DIR}" >&2
  exit 1
fi

if ! Rscript -e "quit(status = if (requireNamespace('renv', quietly = TRUE)) 0 else 1)"; then
  echo "Target image does not have the renv package preinstalled." >&2
  exit 1
fi

SYSTEM_LIBRARY="$(Rscript -e "cat(R.home('library'))")"
PROJECT_DIR="${PROJECTS_ROOT%/}/${APP_NAME}"
PROJECT_LIBRARY="$(Rscript -e "suppressPackageStartupMessages(library(renv)); cat(renv::paths\$library(project='${PROJECT_DIR}'))")"

echo "Preparing Posit-style restore layout"
echo "Bundle directory: ${BUNDLE_DIR}"
echo "Output directory: ${OUTPUT_DIR}"
echo "Project directory: ${PROJECT_DIR}"
echo "Project library: ${PROJECT_LIBRARY}"
echo "Cache root: ${CACHE_ROOT}"
echo "System library: ${SYSTEM_LIBRARY}"

rm -rf "${PROJECT_DIR}" "${CACHE_ROOT}"
mkdir -p "${PROJECT_DIR}" "${CACHE_ROOT}" "${PROJECT_LIBRARY}"

echo "Verifying offline cache checksum"
( cd "${BUNDLE_DIR}" && sha256sum -c "$(basename "${CACHE_CHECKSUM}")" )
echo "Verifying project library checksum"
( cd "${BUNDLE_DIR}" && sha256sum -c "$(basename "${LIBRARY_CHECKSUM}")" )

echo "Extracting offline cache"
tar -xzf "${CACHE_ARCHIVE}" -C "${CACHE_ROOT}"
echo "Extracting realized project library"
tar -xzf "${LIBRARY_ARCHIVE}" -C "${PROJECT_LIBRARY}"

echo "Staging project artifacts"
cp "${LOCK_FILE}" "${PROJECT_DIR}/renv.lock"
cp "${INSTALLED_CSV}" "${PROJECT_DIR}/installed-packages.csv"
cp "${SUMMARY_JSON}" "${PROJECT_DIR}/materialization-summary.json"
cp "${RUN_METADATA_JSON}" "${PROJECT_DIR}/run-metadata.json"

cat > "${PROJECT_DIR}/.Renviron" <<EOF
RENV_PATHS_CACHE=${CACHE_ROOT}
RENV_CONFIG_CACHE_SYMLINKS=FALSE
RENV_CONFIG_EXTERNAL_LIBRARIES=${SYSTEM_LIBRARY}
EOF

export RENV_PATHS_CACHE="${CACHE_ROOT}"
export RENV_CONFIG_CACHE_SYMLINKS=FALSE
export RENV_CONFIG_EXTERNAL_LIBRARIES="${SYSTEM_LIBRARY}"

echo "Running offline renv restore with network disabled"
(
  cd "${PROJECT_DIR}"
  Rscript -e "options(repos=c(CRAN='file:///nonexistent-cran',RSPM='file:///nonexistent-rspm')); stopifnot(requireNamespace('renv', quietly=TRUE)); renv::consent(provided=TRUE); writeLines(.libPaths()); renv::restore(lockfile='renv.lock', prompt=FALSE, clean=TRUE)"
) > "${OUTPUT_DIR}/restore.log" 2>&1

echo "Capturing realized package inventory"
Rscript -e "ip <- as.data.frame(installed.packages(fields=c('Priority','Repository'), noCache=TRUE), stringsAsFactors=FALSE); out <- data.frame(package_name=ip[, 'Package'], package_version=ip[, 'Version'], library_path=ip[, 'LibPath'], priority=if ('Priority' %in% names(ip)) ip[, 'Priority'] else '', repository=if ('Repository' %in% names(ip)) ip[, 'Repository'] else '', stringsAsFactors=FALSE); out <- out[order(tolower(out\$package_name)), ]; write.csv(out, '${OUTPUT_DIR}/enclave-installed-packages.csv', row.names=FALSE)"

echo "Capturing renv status"
(
  cd "${PROJECT_DIR}"
  Rscript -e "renv::status()"
) > "${OUTPUT_DIR}/renv-status.txt" 2>&1

echo "Comparing realized inventory to approved installed-packages.csv"
Rscript -e "approved <- read.csv('${INSTALLED_CSV}', stringsAsFactors=FALSE); realized <- read.csv('${OUTPUT_DIR}/enclave-installed-packages.csv', stringsAsFactors=FALSE); approved <- unique(approved[, c('package_name','package_version')]); realized <- unique(realized[, c('package_name','package_version')]); approved_key <- paste(approved\$package_name, approved\$package_version, sep='=='); realized_key <- paste(realized\$package_name, realized\$package_version, sep='=='); missing <- approved[!(approved_key %in% realized_key), , drop=FALSE]; extras <- realized[!(realized_key %in% approved_key), , drop=FALSE]; write.csv(missing, '${OUTPUT_DIR}/inventory-missing.csv', row.names=FALSE); write.csv(extras, '${OUTPUT_DIR}/inventory-extra.csv', row.names=FALSE); approved_count <- nrow(approved); realized_count <- nrow(realized); restored_count <- suppressWarnings(as.integer(sub('.*: *', '', grep('\"restored_packages\"[[:space:]]*:[[:space:]]*[0-9]+', readLines('${SUMMARY_JSON}', warn=FALSE), value=TRUE)[1]))); cat(sprintf('approved_package_versions=%d\nrealized_package_versions=%d\nmissing_package_versions=%d\nextra_package_versions=%d\napproved_restored_packages=%s\n', approved_count, realized_count, nrow(missing), nrow(extras), ifelse(is.na(restored_count), 'unknown', as.character(restored_count))), file='${OUTPUT_DIR}/verification-summary.txt'); if (nrow(missing) > 0) { quit(status=1) }"

echo "Verification completed successfully"
