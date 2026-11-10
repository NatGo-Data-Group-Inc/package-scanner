#!/usr/bin/env bash
set -euo pipefail

R_VERSION=""
INSTALL_PREFIX="/opt/R"

usage() {
  cat <<'EOF'
Usage: install-r-runtime.sh --r-version <version> [--install-prefix <dir>]
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --r-version) R_VERSION="$2"; shift 2 ;;
    --install-prefix) INSTALL_PREFIX="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${R_VERSION}" ]]; then
  echo "--r-version is required." >&2
  exit 1
fi

TARGET_DIR="${INSTALL_PREFIX%/}/${R_VERSION}"
TARGET_RSCRIPT="${TARGET_DIR}/bin/Rscript"

if command -v Rscript >/dev/null 2>&1; then
  CURRENT_VERSION="$(Rscript -e 'cat(as.character(getRversion()))' 2>/dev/null || true)"
  if [[ "${CURRENT_VERSION}" == "${R_VERSION}" ]]; then
    echo "Using existing Rscript in PATH (${CURRENT_VERSION})"
    exit 0
  fi
fi

if [[ -x "${TARGET_RSCRIPT}" ]]; then
  echo "Using cached R runtime at ${TARGET_RSCRIPT}"
  exit 0
fi

install_build_deps_apt() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    file \
    gfortran \
    gzip \
    libbz2-dev \
    libcairo2-dev \
    libcurl4-openssl-dev \
    libfontconfig1-dev \
    libfribidi-dev \
    libfreetype6-dev \
    libharfbuzz-dev \
    libicu-dev \
    libjpeg-dev \
    liblapack-dev \
    liblzma-dev \
    libpcre2-dev \
    libpng-dev \
    libreadline-dev \
    libssl-dev \
    libtiff5-dev \
    libx11-dev \
    libxt-dev \
    libxml2-dev \
    make \
    tar \
    tk-dev \
    wget \
    xz-utils \
    zlib1g-dev
}

install_build_deps_rpm() {
  local pkg_mgr="$1"
  "${pkg_mgr}" install -y --allowerasing \
    bzip2 \
    bzip2-devel \
    cairo-devel \
    file \
    findutils \
    fontconfig-devel \
    freetype-devel \
    fribidi-devel \
    gcc \
    gcc-c++ \
    gcc-gfortran \
    gzip \
    harfbuzz-devel \
    libX11-devel \
    libXt-devel \
    libcurl-devel \
    libicu-devel \
    libjpeg-turbo-devel \
    libpng-devel \
    libtiff-devel \
    libxml2-devel \
    make \
    openssl-devel \
    pcre2-devel \
    readline-devel \
    tar \
    tk-devel \
    wget \
    which \
    xz \
    xz-devel \
    zlib-devel
}

if command -v apt-get >/dev/null 2>&1; then
  install_build_deps_apt
elif command -v dnf >/dev/null 2>&1; then
  install_build_deps_rpm dnf
elif command -v yum >/dev/null 2>&1; then
  install_build_deps_rpm yum
else
  echo "Unsupported Linux package manager; expected apt-get or yum." >&2
  exit 1
fi

mkdir -p /tmp/r-build "${TARGET_DIR}"
ARCHIVE="/tmp/r-build/R-${R_VERSION}.tar.gz"
SRC_DIR="/tmp/r-build/R-${R_VERSION}"

if [[ ! -f "${ARCHIVE}" ]]; then
  curl --retry 5 --retry-delay 2 --retry-connrefused -fsSL \
    "https://cran.r-project.org/src/base/R-4/R-${R_VERSION}.tar.gz" \
    -o "${ARCHIVE}"
fi

rm -rf "${SRC_DIR}"
tar -xzf "${ARCHIVE}" -C /tmp/r-build

pushd "${SRC_DIR}" >/dev/null
./configure --prefix="${TARGET_DIR}" --enable-R-shlib
make -j"$(nproc)"
make install
popd >/dev/null

echo "Installed R ${R_VERSION} to ${TARGET_DIR}"
