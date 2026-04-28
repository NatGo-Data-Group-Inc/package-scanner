FROM public.ecr.aws/amazonlinux/amazonlinux:2023

ARG TRIVY_VERSION=0.69.3
ARG TARGETARCH

RUN dnf install -y \
    awscli \
    bzip2 \
    file \
    findutils \
    git \
    gzip \
    jq \
    python3 \
    python3-pip \
    tar \
    which \
    xz \
    zip && \
    dnf clean all

RUN case "${TARGETARCH}" in \
      amd64) MAMBA_ARCH="linux-64" ;; \
      arm64) MAMBA_ARCH="linux-aarch64" ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl --retry 5 --retry-delay 2 --retry-connrefused -fsSL "https://micro.mamba.pm/api/micromamba/${MAMBA_ARCH}/latest" \
      | tar -xvj -C /usr/local/bin --strip-components=1 bin/micromamba && \
    chmod +x /usr/local/bin/micromamba

RUN python3 -m pip install --no-cache-dir --ignore-installed boto3 conda-pack cyclonedx-bom pyyaml safety && \
    case "${TARGETARCH}" in \
      amd64) TRIVY_ARCH="64bit" ;; \
      arm64) TRIVY_ARCH="ARM64" ;; \
      *) echo "Unsupported TARGETARCH for Trivy: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl --retry 5 --retry-delay 2 --retry-connrefused -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-${TRIVY_ARCH}.tar.gz" -o /tmp/trivy.tar.gz && \
    tar -xzf /tmp/trivy.tar.gz -C /usr/local/bin trivy

ENV SCRIPT_ROOT=/opt/package-scanner/scripts
ENV PYTHON_BIN=/usr/bin/python3
ENV MAMBA_BIN=/usr/local/bin/micromamba

COPY scripts/bundle-directory.py /opt/package-scanner/scripts/bundle-directory.py
COPY scripts/extract-archive.py /opt/package-scanner/scripts/extract-archive.py
COPY scripts/generate-governance-artifacts.py /opt/package-scanner/scripts/generate-governance-artifacts.py
COPY scripts/generate-python-materialization-summary.py /opt/package-scanner/scripts/generate-python-materialization-summary.py
COPY scripts/run-python-ecs-task.sh /opt/package-scanner/scripts/run-python-ecs-task.sh

RUN chmod +x /opt/package-scanner/scripts/run-python-ecs-task.sh

ENTRYPOINT ["/opt/package-scanner/scripts/run-python-ecs-task.sh"]
