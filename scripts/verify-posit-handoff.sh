#!/usr/bin/env bash
set -euo pipefail

IMAGE=""
EVIDENCE_BUCKET=""
TIMESTAMP=""
PLATFORM="linux-amd64"
REGION="us-east-1"
PROFILE=""
APP_NAME="pi-posit-restore"
WORK_DIR=""
KEEP_WORK_DIR="true"
DOCKER_CONFIG_DIR="${DOCKER_CONFIG:-/tmp/package-scanner-docker-config}"
LOCAL_HOST="false"
BUNDLE_DIR_OVERRIDE=""

usage() {
  cat <<'EOF'
Usage: verify-posit-handoff.sh --image <docker-image> --evidence-bucket <bucket> --timestamp <timestamp> [options]

Download the approved R offline bundle from S3 and verify the Posit handoff
procedure inside the exact Linux image supplied by --image. The container runs
with --network none and follows docs/posit-handoff-runbook.md paths under
/opt/posit/renv/cache/R-4.4.0 and /opt/posit/projects/<application-name>.

For direct execution on a Posit server, use --local-host and either provide a
pre-staged --bundle-dir or let the script download the artifacts locally first.

Required:
  Docker mode:
    --image <docker-image>      Exact target image to verify
    --evidence-bucket <bucket>  Evidence bucket containing the approved artifacts
    --timestamp <timestamp>     Run timestamp, e.g. 20260412T110306Z

  Local-host mode:
    --local-host
    and either:
      --bundle-dir <dir>
    or:
      --evidence-bucket <bucket> --timestamp <timestamp>

Optional:
  --platform <platform>       (default: linux-amd64)
  --region <region>           (default: us-east-1)
  --profile <profile>
  --app-name <name>           (default: pi-posit-restore)
  --work-dir <dir>            (default: ./artifacts/posit-restore-verification/<platform>/<timestamp>)
  --keep-work-dir             Keep downloaded artifacts and outputs (default)
  --bundle-dir <dir>          Reuse a local handoff bundle instead of downloading from S3
  --local-host                Run directly on the current host instead of docker run
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) IMAGE="$2"; shift 2 ;;
    --evidence-bucket) EVIDENCE_BUCKET="$2"; shift 2 ;;
    --timestamp) TIMESTAMP="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --app-name) APP_NAME="$2"; shift 2 ;;
    --work-dir) WORK_DIR="$2"; shift 2 ;;
    --keep-work-dir) KEEP_WORK_DIR="true"; shift 1 ;;
    --bundle-dir) BUNDLE_DIR_OVERRIDE="$2"; shift 2 ;;
    --local-host) LOCAL_HOST="true"; shift 1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ "${LOCAL_HOST}" == "true" ]]; then
  if [[ -z "${BUNDLE_DIR_OVERRIDE}" && ( -z "${EVIDENCE_BUCKET}" || -z "${TIMESTAMP}" ) ]]; then
    echo "Local-host mode requires either --bundle-dir or both --evidence-bucket and --timestamp." >&2
    usage >&2
    exit 1
  fi
else
  if [[ -z "${IMAGE}" || -z "${EVIDENCE_BUCKET}" || -z "${TIMESTAMP}" ]]; then
    echo "Docker mode requires --image, --evidence-bucket, and --timestamp." >&2
    usage >&2
    exit 1
  fi
fi

required_cmds=(mktemp realpath)
if [[ -z "${BUNDLE_DIR_OVERRIDE}" ]]; then
  required_cmds+=(aws)
fi
if [[ "${LOCAL_HOST}" != "true" ]]; then
  required_cmds+=(docker)
fi
for cmd in "${required_cmds[@]}"; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Required command not found: ${cmd}" >&2
    exit 1
  fi
done

AWS_ARGS=(--region "${REGION}")
if [[ -n "${PROFILE}" ]]; then
  AWS_ARGS+=(--profile "${PROFILE}")
fi

mkdir -p "${DOCKER_CONFIG_DIR}"
export DOCKER_CONFIG="${DOCKER_CONFIG_DIR}"

if [[ -z "${TIMESTAMP}" ]]; then
  TIMESTAMP="manual-$(date -u +%Y%m%dT%H%M%SZ)"
fi

if [[ -z "${WORK_DIR}" ]]; then
  WORK_DIR="${PWD}/artifacts/posit-restore-verification/${PLATFORM}/${TIMESTAMP}"
fi

BUNDLE_DIR="${BUNDLE_DIR_OVERRIDE:-${WORK_DIR}/bundle}"
RESULTS_DIR="${WORK_DIR}/results"
mkdir -p "${RESULTS_DIR}"
if [[ -z "${BUNDLE_DIR_OVERRIDE}" ]]; then
  mkdir -p "${BUNDLE_DIR}"
fi

cleanup() {
  if [[ "${KEEP_WORK_DIR}" != "true" ]]; then
    rm -rf "${WORK_DIR}"
  fi
}

trap cleanup EXIT

INSIDE_SCRIPT="${PWD}/scripts/verify-posit-handoff-inside.sh"
if [[ ! -f "${INSIDE_SCRIPT}" ]]; then
  echo "Missing inside-container verifier script: ${INSIDE_SCRIPT}" >&2
  exit 1
fi

download_artifact() {
  local key="$1"
  local target="$2"
  if [[ -f "${target}" ]]; then
    echo "Reusing existing artifact: ${target}"
    return
  fi
  aws s3 cp "s3://${EVIDENCE_BUCKET}/${key}" "${target}" "${AWS_ARGS[@]}"
}

download_artifact_with_fallback() {
  local primary_key="$1"
  local fallback_key="$2"
  local target="$3"
  if [[ -f "${target}" ]]; then
    echo "Reusing existing artifact: ${target}"
    return
  fi
  if aws s3 cp "s3://${EVIDENCE_BUCKET}/${primary_key}" "${target}" "${AWS_ARGS[@]}" >/dev/null 2>&1; then
    return
  fi
  aws s3 cp "s3://${EVIDENCE_BUCKET}/${fallback_key}" "${target}" "${AWS_ARGS[@]}"
}

ensure_docker_image_access() {
  local image_ref="$1"
  local registry="${image_ref%%/*}"

  if [[ "${registry}" == *.dkr.ecr.*.amazonaws.com ]]; then
    echo "Authenticating Docker to ${registry}"
    aws ecr get-login-password "${AWS_ARGS[@]}" | docker login --username AWS --password-stdin "${registry}" >/dev/null
  fi
}

if [[ -z "${BUNDLE_DIR_OVERRIDE}" ]]; then
  REQ_PREFIX="evidence/requirements/r/${PLATFORM}/${TIMESTAMP}"
  TRACE_PREFIX="evidence/traceability/r/${PLATFORM}/${TIMESTAMP}"
  ENV_PREFIX="evidence/env-artifacts/r/${PLATFORM}/${TIMESTAMP}"
  OFFLINE_PREFIX="evidence/packages/offline/r/${PLATFORM}/${TIMESTAMP}"
  CACHE_ARCHIVE_NAME="renv-cache-${PLATFORM}-${TIMESTAMP}.tar.gz"
  LIBRARY_ARCHIVE_NAME="renv-library-${PLATFORM}-${TIMESTAMP}.tar.gz"

  echo "Downloading approved handoff bundle into ${BUNDLE_DIR}"
  download_artifact "${REQ_PREFIX}/renv.lock" "${BUNDLE_DIR}/renv.lock"
  download_artifact "${REQ_PREFIX}/installed-packages.csv" "${BUNDLE_DIR}/installed-packages.csv"
  download_artifact "${TRACE_PREFIX}/materialization-summary.json" "${BUNDLE_DIR}/materialization-summary.json"
  download_artifact "${TRACE_PREFIX}/run-metadata.json" "${BUNDLE_DIR}/run-metadata.json"
  download_artifact "${OFFLINE_PREFIX}/${CACHE_ARCHIVE_NAME}" "${BUNDLE_DIR}/${CACHE_ARCHIVE_NAME}"
  download_artifact "${OFFLINE_PREFIX}/${CACHE_ARCHIVE_NAME}.sha256" "${BUNDLE_DIR}/${CACHE_ARCHIVE_NAME}.sha256"
  download_artifact_with_fallback "${OFFLINE_PREFIX}/${LIBRARY_ARCHIVE_NAME}" "${ENV_PREFIX}/${LIBRARY_ARCHIVE_NAME}" "${BUNDLE_DIR}/${LIBRARY_ARCHIVE_NAME}"
  download_artifact_with_fallback "${OFFLINE_PREFIX}/${LIBRARY_ARCHIVE_NAME}.sha256" "${ENV_PREFIX}/${LIBRARY_ARCHIVE_NAME}.sha256" "${BUNDLE_DIR}/${LIBRARY_ARCHIVE_NAME}.sha256"
else
  BUNDLE_DIR="$(realpath "${BUNDLE_DIR}")"
  echo "Using local handoff bundle from ${BUNDLE_DIR}"
fi

if [[ "${LOCAL_HOST}" == "true" ]]; then
  echo "Running Posit handoff verification directly on the current host"
  "${INSIDE_SCRIPT}" \
    --bundle-dir "${BUNDLE_DIR}" \
    --output-dir "${RESULTS_DIR}" \
    --app-name "${APP_NAME}"
else
  ensure_docker_image_access "${IMAGE}"

  echo "Launching offline restore verification in ${IMAGE}"
  docker run --rm --network none \
    --entrypoint /bin/bash \
    -v "$(realpath "${BUNDLE_DIR}"):/bundle:ro" \
    -v "$(realpath "${RESULTS_DIR}"):/results:rw" \
    -v "$(realpath "${INSIDE_SCRIPT}"):/opt/package-scanner/verify-posit-handoff-inside.sh:ro" \
    "${IMAGE}" \
    /opt/package-scanner/verify-posit-handoff-inside.sh \
      --bundle-dir /bundle \
      --output-dir /results \
      --app-name "${APP_NAME}"
fi

echo "Posit restore verification succeeded"
echo "Artifacts:"
echo "  bundle:  ${BUNDLE_DIR}"
echo "  results: ${RESULTS_DIR}"
echo "  summary: ${RESULTS_DIR}/verification-summary.txt"
