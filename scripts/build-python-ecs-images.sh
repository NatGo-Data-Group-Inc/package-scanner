#!/usr/bin/env bash
set -euo pipefail

STACK_NAME=""
REGION="us-east-1"
PROFILE=""
ALLOW_DEFAULT_PROFILE="false"
PLATFORMS="linux/amd64,linux/arm64"
IMAGE_TAG=""
PUSH_LATEST="true"

usage() {
  cat <<'EOF'
Usage: build-python-ecs-images.sh --stack-name <name> [options]

Options:
  --stack-name <name>
  --region <region>                   (default: us-east-1)
  --profile <profile>
  --allow-default-profile
  --platforms <csv>                   (default: linux/amd64,linux/arm64)
  --tag <tag>                         (default: UTC timestamp + short git sha when available)
  --no-latest                         Do not also refresh the latest tag
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --allow-default-profile) ALLOW_DEFAULT_PROFILE="true"; shift 1 ;;
    --platforms) PLATFORMS="$2"; shift 2 ;;
    --tag) IMAGE_TAG="$2"; shift 2 ;;
    --no-latest) PUSH_LATEST="false"; shift 1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${STACK_NAME}" ]]; then
  echo "--stack-name is required." >&2
  exit 1
fi
if [[ "${ALLOW_DEFAULT_PROFILE}" != "true" && -z "${PROFILE}" ]]; then
  echo "Guardrail: --profile is required unless --allow-default-profile is explicitly set." >&2
  exit 1
fi

AWS_ARGS=(--region "${REGION}")
if [[ -n "${PROFILE}" ]]; then
  AWS_ARGS+=(--profile "${PROFILE}")
fi

if [[ -z "${DOCKER_CONFIG:-}" ]]; then
  DOCKER_CONFIG="/tmp/package-scanner-docker-config"
  export DOCKER_CONFIG
fi
mkdir -p "${DOCKER_CONFIG}"
if [[ ! -x "${DOCKER_CONFIG}/cli-plugins/docker-buildx" && -x "${HOME}/.docker/cli-plugins/docker-buildx" ]]; then
  mkdir -p "${DOCKER_CONFIG}/cli-plugins"
  cp "${HOME}/.docker/cli-plugins/docker-buildx" "${DOCKER_CONFIG}/cli-plugins/docker-buildx"
  chmod +x "${DOCKER_CONFIG}/cli-plugins/docker-buildx"
fi
if ! docker buildx version >/dev/null 2>&1; then
  echo "docker buildx is required. Install the buildx CLI plugin first." >&2
  exit 1
fi

BUILDER_NAME="package-scanner-python-builder"
if ! docker buildx inspect "${BUILDER_NAME}" >/dev/null 2>&1; then
  docker buildx create --name "${BUILDER_NAME}" --use >/dev/null
else
  docker buildx use "${BUILDER_NAME}" >/dev/null
fi
docker buildx inspect "${BUILDER_NAME}" --bootstrap >/dev/null

stack_output() {
  local key="$1"
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue | [0]" \
    --output text \
    "${AWS_ARGS[@]}"
}

LINUX_REPO="$(stack_output PythonLinuxRepositoryUri)"
if [[ -z "${LINUX_REPO}" || "${LINUX_REPO}" == "None" ]]; then
  echo "Missing output: PythonLinuxRepositoryUri" >&2
  exit 1
fi
if [[ -z "${IMAGE_TAG}" ]]; then
  GIT_SHA="$(git rev-parse --short=12 HEAD 2>/dev/null || true)"
  if [[ -n "${GIT_SHA}" ]]; then
    IMAGE_TAG="$(date -u +%Y%m%dT%H%M%SZ)-${GIT_SHA}"
  else
    IMAGE_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
  fi
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text "${AWS_ARGS[@]}")"
aws ecr get-login-password "${AWS_ARGS[@]}" | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

TAGS=(-t "${LINUX_REPO}:${IMAGE_TAG}")
if [[ "${PUSH_LATEST}" == "true" ]]; then
  TAGS+=(-t "${LINUX_REPO}:latest")
fi

docker buildx build \
  --platform "${PLATFORMS}" \
  -f docker/python-linux.Dockerfile \
  "${TAGS[@]}" \
  --push \
  .

echo "Python Linux image pushed: ${LINUX_REPO}:${IMAGE_TAG}"
if [[ "${PUSH_LATEST}" == "true" ]]; then
  echo "Python Linux latest tag refreshed: ${LINUX_REPO}:latest"
fi
