#!/usr/bin/env bash
set -euo pipefail

REGION="us-east-1"
PROFILE=""
ALLOW_DEFAULT_PROFILE="false"
STACK_NAME="package-scanner-webapp-dev"
IMAGE_TAG=""

usage() {
  cat <<'EOF'
Usage: build-webapp-image.sh [options]

Options:
  --region <region>
  --profile <profile>
  --allow-default-profile
  --stack-name <name>
  --image-tag <tag>      (default: current UTC timestamp)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --allow-default-profile) ALLOW_DEFAULT_PROFILE="true"; shift 1 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --image-tag) IMAGE_TAG="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ "${ALLOW_DEFAULT_PROFILE}" != "true" && -z "${PROFILE}" ]]; then
  echo "Guardrail: --profile is required unless --allow-default-profile is explicitly set." >&2
  exit 1
fi

AWS_ARGS=(--region "${REGION}")
if [[ -n "${PROFILE}" ]]; then
  AWS_ARGS+=(--profile "${PROFILE}")
fi

if [[ -z "${IMAGE_TAG}" ]]; then
  IMAGE_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
fi

REPO_URI="$(
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='WebappRepositoryUri'].OutputValue | [0]" \
    --output text \
    "${AWS_ARGS[@]}"
)"
if [[ -z "${REPO_URI}" || "${REPO_URI}" == "None" ]]; then
  echo "Could not resolve WebappRepositoryUri from stack ${STACK_NAME}" >&2
  exit 1
fi

aws ecr get-login-password "${AWS_ARGS[@]}" | docker login --username AWS --password-stdin "${REPO_URI%%/*}"
docker build -f docker/webapp.Dockerfile -t "${REPO_URI}:${IMAGE_TAG}" .
docker push "${REPO_URI}:${IMAGE_TAG}"
docker tag "${REPO_URI}:${IMAGE_TAG}" "${REPO_URI}:latest"
docker push "${REPO_URI}:latest"

printf '%s\n' "${REPO_URI}:${IMAGE_TAG}"
