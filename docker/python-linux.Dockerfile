# Every tool version below is pinned to what is in the tested scan image (task
# revision 40). Unpinned, a rebuild silently changes the scanner's own
# toolchain: an unpinned micromamba is what changed the conda-list.json format
# and made every materialize run report FAILED. Change a pin deliberately and
# validate with a test scan.

# rockylinux:8 as of Nov 2023 (glibc 2.28, matching the MIP target).
FROM rockylinux:8@sha256:9794037624aaa6212aeada1d28861ef5e0a935adaf93e4ef79837119f2a2d04c

ARG TRIVY_VERSION=0.69.3
ARG MICROMAMBA_VERSION=2.9.0-0
ARG TARGETARCH

# Requested: bzip2 ca-certificates curl file findutils gcc gcc-c++ git gzip jq
# krb5-devel make pkgconf-pkg-config postgresql-devel (provided by libpq-devel)
# python3.11 python3.11-pip tar unzip unixODBC-devel which xz zip.
# The lock holds those plus every dependency, at exact versions.
COPY docker/python-linux.rpm-lock.txt /tmp/python-linux.rpm-lock.txt
RUN dnf install -y $(grep -v '^#' /tmp/python-linux.rpm-lock.txt) && \
    dnf clean all && \
    rm -rf /var/cache/dnf /tmp/python-linux.rpm-lock.txt

RUN case "${TARGETARCH}" in \
      amd64) MAMBA_ARCH="linux-64";      MAMBA_SHA256="366cd9cd8be14df1ab8ed50352a82111082a36686b2d389fdb79a92c3fafb3e3" ;; \
      arm64) MAMBA_ARCH="linux-aarch64"; MAMBA_SHA256="9f93b974adcb4d166996af969b6cd371287d1a3e52733704727884d9b74cb7a7" ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl --retry 5 --retry-delay 2 --retry-connrefused -fsSL \
      "https://github.com/mamba-org/micromamba-releases/releases/download/${MICROMAMBA_VERSION}/micromamba-${MAMBA_ARCH}" \
      -o /usr/local/bin/micromamba && \
    echo "${MAMBA_SHA256}  /usr/local/bin/micromamba" | sha256sum -c - && \
    chmod +x /usr/local/bin/micromamba

COPY docker/python-tools.constraints.txt /tmp/python-tools.constraints.txt
RUN /usr/bin/python3.11 -m pip install --no-cache-dir --ignore-installed \
      -c /tmp/python-tools.constraints.txt \
      awscli boto3 conda-pack cyclonedx-bom pyyaml safety && \
    rm -f /tmp/python-tools.constraints.txt && \
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
