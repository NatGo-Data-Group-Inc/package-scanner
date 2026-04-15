FROM public.ecr.aws/amazonlinux/amazonlinux:2023

ARG R_VERSION=4.4.0
ARG TRIVY_VERSION=0.69.3

RUN dnf install -y \
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
    gdal310-devel \
    geos-devel \
    git \
    gmp-devel \
    gzip \
    harfbuzz-devel \
    ImageMagick-c++-devel \
    ImageMagick-devel \
    java-17-amazon-corretto-devel \
    jq \
    libX11-devel \
    libXt-devel \
    libcurl-devel \
    libgit2-devel \
    libicu-devel \
    libjpeg-turbo-devel \
    libpng-devel \
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
    python3 \
    python3-pip \
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

RUN GDAL_CONFIG_BIN="$(command -v gdal-config || command -v gdal310-config || command -v gdal-config-3.10 || true)" && \
    GDAL_CONFIG_64_BIN="$(command -v gdal-config-64 || command -v gdal310-config-64 || true)" && \
    test -n "${GDAL_CONFIG_BIN}" && \
    test -n "${GDAL_CONFIG_64_BIN}" && \
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
    ./configure --prefix=/opt/R/${R_VERSION} --enable-R-shlib && \
    make -j"$(nproc)" && \
    make install && \
    ln -s /opt/R/${R_VERSION}/bin/R /usr/local/bin/R && \
    ln -s /opt/R/${R_VERSION}/bin/Rscript /usr/local/bin/Rscript

RUN python3 -m pip install --no-cache-dir boto3 && \
    curl --retry 5 --retry-delay 2 --retry-connrefused -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" -o /tmp/trivy.tar.gz && \
    tar -xzf /tmp/trivy.tar.gz -C /usr/local/bin trivy && \
    Rscript -e "options(repos=c(RSPM='https://packagemanager.posit.co/all/latest',CRAN='https://cloud.r-project.org')); install.packages(c('renv','ggplot2','isoband','rlang','vctrs'), dependencies=NA)"

ENV SCRIPT_ROOT=/opt/package-scanner/scripts
ENV PATH=/opt/R/4.4.0/bin:$PATH
ENV R_MAKEVARS_USER=/opt/package-scanner/config/Makevars
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
