FROM rockylinux:8

ARG R_VERSION=4.4.0
ARG PYTHON_VERSION=3.11.11
ARG TRIVY_VERSION=0.69.3
ARG LIBGIT2_VERSION=1.9.1

RUN dnf install -y \
    dnf-plugins-core \
    epel-release && \
    dnf config-manager --set-enabled powertools && \
    dnf install -y \
    awscli \
    bzip2 \
    bzip2-devel \
    cairo-devel \
    cmake \
    file \
    findutils \
    fontconfig-devel \
    freetype-devel \
    fribidi-devel \
    gcc \
    gcc-c++ \
    gcc-gfortran \
    gcc-toolset-12 \
    gcc-toolset-12-gcc \
    gcc-toolset-12-gcc-c++ \
    gcc-toolset-12-gcc-gfortran \
    gdal-devel \
    geos-devel \
    git \
    gmp-devel \
    gzip \
    harfbuzz-devel \
    ImageMagick-c++-devel \
    ImageMagick-devel \
    java-17-openjdk-devel \
    jq \
    libX11-devel \
    libXt-devel \
    libcurl-devel \
    libffi-devel \
    libicu-devel \
    libjpeg-turbo-devel \
    libpng-devel \
    libssh2-devel \
    libuv-devel \
    mpfr-devel \
    libwebp-devel \
    libtiff-devel \
    libxml2-devel \
    make \
    openssl-devel \
    pcre2-devel \
    pkgconfig \
    proj-devel \
    udunits2-devel \
    readline-devel \
    sqlite-devel \
    tar \
    tk-devel \
    unixODBC-devel \
    wget \
    which \
    xz \
    xz-devel \
    zeromq-devel \
    zlib-devel && \
    dnf clean all

ENV PATH=/opt/rh/gcc-toolset-12/root/usr/bin:/opt/R/4.4.0/bin:$PATH
ENV LD_LIBRARY_PATH=/opt/rh/gcc-toolset-12/root/usr/lib64:/usr/local/lib64:/usr/local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}
ENV CC=/opt/rh/gcc-toolset-12/root/usr/bin/gcc
ENV CXX=/opt/rh/gcc-toolset-12/root/usr/bin/g++
ENV FC=/opt/rh/gcc-toolset-12/root/usr/bin/gfortran
ENV F77=/opt/rh/gcc-toolset-12/root/usr/bin/gfortran
ENV PKG_CONFIG_PATH=/usr/local/lib64/pkgconfig:/usr/local/lib/pkgconfig${PKG_CONFIG_PATH:+:${PKG_CONFIG_PATH}}

RUN mkdir -p /tmp/python-build && \
    curl --retry 5 --retry-delay 2 --retry-connrefused -fsSL "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tgz" -o /tmp/python-build/Python-${PYTHON_VERSION}.tgz && \
    tar -xzf /tmp/python-build/Python-${PYTHON_VERSION}.tgz -C /tmp/python-build && \
    cd /tmp/python-build/Python-${PYTHON_VERSION} && \
    ./configure --prefix=/usr/local --with-ensurepip=install && \
    make -j"$(nproc)" && \
    make altinstall && \
    /usr/local/bin/python3.11 -m pip install --no-cache-dir --upgrade pip setuptools wheel

RUN mkdir -p /tmp/libgit2-build && \
    curl --retry 5 --retry-delay 2 --retry-connrefused -fsSL "https://github.com/libgit2/libgit2/archive/refs/tags/v${LIBGIT2_VERSION}.tar.gz" -o /tmp/libgit2-build/libgit2-${LIBGIT2_VERSION}.tar.gz && \
    tar -xzf /tmp/libgit2-build/libgit2-${LIBGIT2_VERSION}.tar.gz -C /tmp/libgit2-build && \
    cmake -S /tmp/libgit2-build/libgit2-${LIBGIT2_VERSION} -B /tmp/libgit2-build/build \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/usr/local \
      -DBUILD_SHARED_LIBS=ON \
      -DUSE_SSH=ON \
      -DREGEX_BACKEND=builtin && \
    cmake --build /tmp/libgit2-build/build -j"$(nproc)" && \
    cmake --install /tmp/libgit2-build/build && \
    ldconfig

RUN GDAL_CONFIG_BIN="$(command -v gdal-config || command -v gdal310-config || command -v gdal-config-3.10 || true)" && \
    test -n "${GDAL_CONFIG_BIN}" && \
    GDAL_CONFIG_64_BIN="$(command -v gdal-config-64 || command -v gdal310-config-64 || echo "${GDAL_CONFIG_BIN}")" && \
    ln -sf "${GDAL_CONFIG_64_BIN}" /usr/local/bin/gdal-config-64 && \
    ln -sf "${GDAL_CONFIG_BIN}" /usr/local/bin/gdal-config

RUN cat > /usr/local/bin/udunits2-config <<'EOF' && chmod +x /usr/local/bin/udunits2-config
#!/usr/bin/env bash
set -euo pipefail

prefix="/usr"
includedir="${prefix}/include"
libdir="${prefix}/lib64"
version="$(rpm -q --queryformat '%{VERSION}\n' udunits2-devel 2>/dev/null || echo unknown)"

case "${1:-}" in
  --prefix)
    printf '%s\n' "${prefix}"
    ;;
  --includedir)
    printf '%s\n' "${includedir}"
    ;;
  --libdir)
    printf '%s\n' "${libdir}"
    ;;
  --cflags)
    printf '%s\n' "-I${includedir}"
    ;;
  --libs)
    printf '%s\n' "-L${libdir} -ludunits2"
    ;;
  --version)
    printf '%s\n' "${version}"
    ;;
  "")
    printf '%s\n' "${version}"
    ;;
  *)
    printf 'Unsupported option: %s\n' "${1}" >&2
    exit 1
    ;;
esac
EOF
RUN mkdir -p /tmp/r-build /opt/R && \
    curl --retry 5 --retry-delay 2 --retry-connrefused -fsSL "https://cran.r-project.org/src/base/R-4/R-${R_VERSION}.tar.gz" -o /tmp/r-build/R-${R_VERSION}.tar.gz && \
    tar -xzf /tmp/r-build/R-${R_VERSION}.tar.gz -C /tmp/r-build && \
    cd /tmp/r-build/R-${R_VERSION} && \
    ./configure --prefix=/opt/R/${R_VERSION} --libdir=/opt/R/${R_VERSION}/lib --enable-R-shlib && \
    make -j"$(nproc)" && \
    make install && \
    ln -s /opt/R/${R_VERSION}/bin/R /usr/local/bin/R && \
    ln -s /opt/R/${R_VERSION}/bin/Rscript /usr/local/bin/Rscript

RUN /usr/local/bin/python3.11 -m pip install --no-cache-dir boto3 && \
    curl --retry 5 --retry-delay 2 --retry-connrefused -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" -o /tmp/trivy.tar.gz && \
    tar -xzf /tmp/trivy.tar.gz -C /usr/local/bin trivy && \
    Rscript -e "options(repos=c(RSPM='https://packagemanager.posit.co/all/latest',CRAN='https://cloud.r-project.org')); install.packages(c('renv','BiocManager','ggplot2','isoband','rlang','vctrs'), dependencies=NA)"

ENV SCRIPT_ROOT=/opt/package-scanner/scripts
ENV PYTHON_BIN=/usr/local/bin/python3.11
ENV R_MAKEVARS_USER=/opt/package-scanner/config/Makevars
ENV R_SYSTEM_LIBRARY=/opt/R/4.4.0/lib/R/library
ENV DOWNLOAD_STATIC_LIBV8=1

COPY docker/r-linux.Makevars /opt/package-scanner/config/Makevars
COPY scripts/materialize-r-environment.R /opt/package-scanner/scripts/materialize-r-environment.R
COPY scripts/bundle-directory.py /opt/package-scanner/scripts/bundle-directory.py
COPY scripts/extract-archive.py /opt/package-scanner/scripts/extract-archive.py
COPY scripts/generate-r-materialization-summary.py /opt/package-scanner/scripts/generate-r-materialization-summary.py
COPY scripts/generate-r-sbom.py /opt/package-scanner/scripts/generate-r-sbom.py
COPY scripts/scan-r-vulnerabilities.py /opt/package-scanner/scripts/scan-r-vulnerabilities.py
COPY scripts/preflight-r-native-deps.py /opt/package-scanner/scripts/preflight-r-native-deps.py
COPY scripts/generate-r-governance-artifacts.py /opt/package-scanner/scripts/generate-r-governance-artifacts.py
COPY scripts/run-r-ecs-task.sh /opt/package-scanner/scripts/run-r-ecs-task.sh

RUN chmod +x /opt/package-scanner/scripts/run-r-ecs-task.sh

ENTRYPOINT ["/opt/package-scanner/scripts/run-r-ecs-task.sh"]
