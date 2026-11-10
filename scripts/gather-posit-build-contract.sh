#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${1:-/var/tmp/posit-r-baseline}"
ARCHIVE_NAME="$(basename "${OUTPUT_DIR}")-manual.tgz"
MANUAL_DIR="${OUTPUT_DIR}/manual"

mkdir -p "${MANUAL_DIR}"

echo "Writing Posit build-contract data to ${MANUAL_DIR}"

cat /etc/os-release > "${MANUAL_DIR}/os-release.txt"
uname -a > "${MANUAL_DIR}/uname.txt"
ldd --version > "${MANUAL_DIR}/ldd-version.txt" 2>&1
rpm -qa | sort > "${MANUAL_DIR}/rpm-inventory-full.txt"
gcc --version > "${MANUAL_DIR}/gcc-version.txt" 2>&1
g++ --version > "${MANUAL_DIR}/gpp-version.txt" 2>&1
gfortran --version > "${MANUAL_DIR}/gfortran-version.txt" 2>&1
make --version > "${MANUAL_DIR}/make-version.txt" 2>&1

Rscript --vanilla -e "sessionInfo(); cat('\nR.home=', R.home(), '\n'); cat('R.library=', R.home('library'), '\n'); cat('platform=', R.version\$platform, '\n')" \
  > "${MANUAL_DIR}/r-session-and-paths.txt" 2>&1

Rscript --vanilla -e "ip <- as.data.frame(installed.packages(priority=c('base','recommended'))[,c('Package','Version','Priority','LibPath')]); write.csv(ip, '${MANUAL_DIR}/base-recommended.csv', row.names=FALSE)"

Rscript --vanilla -e "ip <- as.data.frame(installed.packages()[,c('Package','Version','Priority','LibPath')]); write.csv(ip, '${MANUAL_DIR}/all-installed.csv', row.names=FALSE)"

Rscript --vanilla -e "writeLines(.libPaths(), '${MANUAL_DIR}/libpaths.txt')"

tar -czf "${OUTPUT_DIR}/${ARCHIVE_NAME}" -C "${OUTPUT_DIR}" manual

echo "Done."
echo "Directory: ${MANUAL_DIR}"
echo "Archive: ${OUTPUT_DIR}/${ARCHIVE_NAME}"
