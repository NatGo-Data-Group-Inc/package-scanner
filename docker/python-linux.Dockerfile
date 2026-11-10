FROM rockylinux:8

ARG TRIVY_VERSION=0.69.3
ARG TARGETARCH

RUN dnf install -y \
    bzip2 \
    ca-certificates \
    curl \
    file \
    findutils \
    gcc \
    gcc-c++ \
    git \
    gzip \
    jq \
    krb5-devel \
    make \
    pkgconf-pkg-config \
    postgresql-devel \
    python3.11 \
    python3.11-pip \
    tar \
    unzip \
    unixODBC-devel \
    which \
    xz \
    zip && \
    dnf clean all && \
    rm -rf /var/cache/dnf

RUN case "${TARGETARCH}" in \
      amd64) MAMBA_ARCH="linux-64" ;; \
      arm64) MAMBA_ARCH="linux-aarch64" ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl --retry 5 --retry-delay 2 --retry-connrefused -fsSL "https://micro.mamba.pm/api/micromamba/${MAMBA_ARCH}/latest" \
      | tar -xvj -C /usr/local/bin --strip-components=1 bin/micromamba && \
    chmod +x /usr/local/bin/micromamba

RUN /usr/bin/python3.11 -m pip install --no-cache-dir --ignore-installed awscli boto3 conda-pack cyclonedx-bom pyyaml safety && \
    case "${TARGETARCH}" in \
      amd64) TRIVY_ARCH="64bit" ;; \
      arm64) TRIVY_ARCH="ARM64" ;; \
      *) echo "Unsupported TARGETARCH for Trivy: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl --retry 5 --retry-delay 2 --retry-connrefused -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-${TRIVY_ARCH}.tar.gz" -o /tmp/trivy.tar.gz && \
    tar -xzf /tmp/trivy.tar.gz -C /usr/local/bin trivy

ENV SCRIPT_ROOT=/opt/package-scanner/scripts
ENV PYTHON_BIN=/usr/bin/python3.11
ENV MAMBA_BIN=/usr/local/bin/micromamba

COPY scripts/bundle-directory.py /opt/package-scanner/scripts/bundle-directory.py
COPY scripts/check-requirements-pinned.py /opt/package-scanner/scripts/check-requirements-pinned.py
COPY scripts/extract-archive.py /opt/package-scanner/scripts/extract-archive.py
COPY scripts/generate-governance-artifacts.py /opt/package-scanner/scripts/generate-governance-artifacts.py
COPY scripts/generate-python-materialization-summary.py /opt/package-scanner/scripts/generate-python-materialization-summary.py
COPY scripts/plan-python-environment-install.py /opt/package-scanner/scripts/plan-python-environment-install.py
COPY scripts/preflight-conda-environment.py /opt/package-scanner/scripts/preflight-conda-environment.py
COPY scripts/run-python-ecs-task.sh /opt/package-scanner/scripts/run-python-ecs-task.sh
COPY package_scanner /opt/package-scanner/package_scanner

ENV PYTHONPATH=/opt/package-scanner

RUN chmod +x /opt/package-scanner/scripts/run-python-ecs-task.sh

ENTRYPOINT ["/opt/package-scanner/scripts/run-python-ecs-task.sh"]
