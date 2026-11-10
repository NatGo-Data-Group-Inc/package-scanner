#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR=""
ARCHIVE="true"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
HOSTNAME_VALUE="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown-host)"

usage() {
  cat <<'EOF'
Usage: collect-posit-r-baseline.sh [--output-dir <dir>] [--archive true|false]

Collect the R runtime baseline from a Linux Posit host so it can be compared
against the scanner/materialization image before approving an air-gapped restore.

Outputs:
  - baseline-summary.txt
  - session-info.txt
  - r-runtime.json
  - posit-base-recommended-packages.csv
  - posit-all-installed-packages.csv
  - libpaths.txt
  - os-release.txt
  - uname.txt
  - toolchain.txt
  - rpm-inventory.txt or dpkg-inventory.txt when available
  - optional tarball of the output directory
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --archive) ARCHIVE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if ! command -v Rscript >/dev/null 2>&1; then
  echo "Rscript not found in PATH." >&2
  exit 1
fi

if [[ -z "${OUTPUT_DIR}" ]]; then
  OUTPUT_DIR="${PWD}/posit-r-baseline-${HOSTNAME_VALUE}-${TIMESTAMP}"
fi

mkdir -p "${OUTPUT_DIR}"

capture_cmd() {
  local out_file="$1"
  shift
  {
    echo "\$ $*"
    "$@"
  } >"${out_file}" 2>&1 || true
}

capture_shell() {
  local out_file="$1"
  local cmd="$2"
  {
    echo "\$ ${cmd}"
    bash -lc "${cmd}"
  } >"${out_file}" 2>&1 || true
}

cat > "${OUTPUT_DIR}/baseline-summary.txt" <<EOF
host=${HOSTNAME_VALUE}
collected_at_utc=${TIMESTAMP}
collector_script=$(basename "$0")
EOF

capture_cmd "${OUTPUT_DIR}/session-info.txt" \
  Rscript -e "sessionInfo()"

capture_cmd "${OUTPUT_DIR}/libpaths.txt" \
  Rscript -e "cat('.libPaths():\n'); writeLines(.libPaths()); cat('\nR.home(library):\n'); cat(R.home('library'), '\n')"

capture_cmd "${OUTPUT_DIR}/r-version.txt" \
  Rscript -e "cat(as.character(getRversion()), '\n')"

capture_cmd "${OUTPUT_DIR}/r-home.txt" \
  Rscript -e "cat(R.home(), '\n')"

capture_cmd "${OUTPUT_DIR}/r-runtime.json" \
  Rscript -e "q <- function(x) sprintf('\"%s\"', gsub('\\\\\"', '\\\\\\\\\"', x)); cat('{\n'); cat(sprintf('  \"r_version\": %s,\n', q(as.character(getRversion())))); cat(sprintf('  \"platform\": %s,\n', q(R.version[['platform']]))); cat(sprintf('  \"arch\": %s,\n', q(R.version[['arch']]))); cat(sprintf('  \"os\": %s,\n', q(R.version[['os']]))); cat(sprintf('  \"system\": %s,\n', q(R.version[['system']]))); cat(sprintf('  \"major\": %s,\n', q(R.version[['major']]))); cat(sprintf('  \"minor\": %s,\n', q(R.version[['minor']]))); cat(sprintf('  \"r_home\": %s,\n', q(R.home()))); cat(sprintf('  \"library_home\": %s,\n', q(R.home('library')))); cat('  \"lib_paths\": [\n'); for (i in seq_along(.libPaths())) { suffix <- if (i < length(.libPaths())) ',' else ''; cat(sprintf('    %s%s\n', q(.libPaths()[[i]]), suffix)); }; cat('  ]\n}\n')"

capture_cmd "${OUTPUT_DIR}/posit-base-recommended-packages.csv" \
  Rscript -e "ip <- as.data.frame(installed.packages(priority=c('base','recommended'))[,c('Package','Version','Priority','LibPath')]); write.csv(ip, row.names=FALSE)"

capture_cmd "${OUTPUT_DIR}/posit-all-installed-packages.csv" \
  Rscript -e "ip <- as.data.frame(installed.packages()[,c('Package','Version','Priority','LibPath')]); write.csv(ip, row.names=FALSE)"

capture_cmd "${OUTPUT_DIR}/recommended-packages.txt" \
  Rscript -e "ip <- installed.packages(priority='recommended')[,c('Package','Version'), drop=FALSE]; write.table(ip, row.names=FALSE, col.names=TRUE, quote=FALSE, sep=',')"

capture_cmd "${OUTPUT_DIR}/base-packages.txt" \
  Rscript -e "ip <- installed.packages(priority='base')[,c('Package','Version'), drop=FALSE]; write.table(ip, row.names=FALSE, col.names=TRUE, quote=FALSE, sep=',')"

capture_cmd "${OUTPUT_DIR}/find-package-MASS.txt" \
  Rscript -e "if ('MASS' %in% rownames(installed.packages())) cat(find.package('MASS'), '\n') else cat('MASS not installed\n')"

capture_cmd "${OUTPUT_DIR}/find-package-renv.txt" \
  Rscript -e "if ('renv' %in% rownames(installed.packages())) cat(find.package('renv'), '\n') else cat('renv not installed\n')"

capture_shell "${OUTPUT_DIR}/os-release.txt" "cat /etc/os-release"
capture_shell "${OUTPUT_DIR}/uname.txt" "uname -a"
capture_shell "${OUTPUT_DIR}/toolchain.txt" "gcc --version; echo; g++ --version; echo; gfortran --version; echo; make --version; echo; ld --version | head -n 5"
capture_shell "${OUTPUT_DIR}/library-linkage-R.txt" "command -v R && ldd \"\$(command -v R)\""
capture_shell "${OUTPUT_DIR}/library-linkage-Rscript.txt" "command -v Rscript && ldd \"\$(command -v Rscript)\""

if command -v rpm >/dev/null 2>&1; then
  capture_shell "${OUTPUT_DIR}/rpm-inventory.txt" "rpm -qa | sort"
fi

if command -v dpkg-query >/dev/null 2>&1; then
  capture_shell "${OUTPUT_DIR}/dpkg-inventory.txt" "dpkg-query -W"
fi

if command -v /usr/sbin/rstudio-server >/dev/null 2>&1; then
  capture_cmd "${OUTPUT_DIR}/posit-workbench-version.txt" /usr/sbin/rstudio-server version
elif command -v rstudio-server >/dev/null 2>&1; then
  capture_cmd "${OUTPUT_DIR}/posit-workbench-version.txt" rstudio-server version
fi

if [[ "${ARCHIVE}" == "true" ]]; then
  archive_path="${OUTPUT_DIR}.tar.gz"
  tar -czf "${archive_path}" -C "$(dirname "${OUTPUT_DIR}")" "$(basename "${OUTPUT_DIR}")"
  printf 'Created %s\n' "${archive_path}"
fi

printf 'Collected baseline in %s\n' "${OUTPUT_DIR}"
