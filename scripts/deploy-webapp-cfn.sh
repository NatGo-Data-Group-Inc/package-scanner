#!/usr/bin/env bash
set -euo pipefail

REGION="us-east-1"
PROFILE=""
ALLOW_DEFAULT_PROFILE="false"
EXPECTED_ACCOUNT_ID=""
STACK_NAME="package-scanner-webapp-dev"
ENVIRONMENT_NAME="package-scanner-dev"
PYTHON_STACK_NAME="cyber-scanner-dev-python-ecs"
R_STACK_NAME="cyber-scanner-dev-r-ecs"
CATALOG_BUCKET_NAME=""
INPUT_BUCKET_NAME=""
EPHEMERAL_BUCKET_NAME=""
CATALOG_PREFIX="evidence"
EPHEMERAL_PREFIX="deploy/tmp/r"
ALLOWED_INGRESS_CIDR="0.0.0.0/0"
TLS_CERTIFICATE_ARN=""
CUSTOM_DOMAIN_NAME=""
CUSTOM_DOMAIN_HOSTED_ZONE_ID=""
RUNTIME_ENABLED="false"
IDLE_TIMEOUT_MINUTES="60"
DESIRED_COUNT="0"
TASK_CPU="1024"
TASK_MEMORY="2048"
IMAGE_TAG=""
SKIP_IMAGE_BUILD="false"
TEMPLATE_S3_BUCKET=""

usage() {
  cat <<'EOF'
Usage: deploy-webapp-cfn.sh [options]

Options:
  --region <region>
  --profile <profile>
  --allow-default-profile
  --expected-account-id <account-id>
  --stack-name <name>
  --environment-name <name>
  --python-stack-name <name>
  --r-stack-name <name>
  --catalog-bucket-name <bucket>
  --input-bucket-name <bucket>
  --ephemeral-bucket-name <bucket>
  --catalog-prefix <prefix>
  --ephemeral-prefix <prefix>
  --allowed-ingress-cidr <cidr>
  --tls-certificate-arn <arn>
  --custom-domain-name <name>
  --custom-domain-hosted-zone-id <zone-id>
  --runtime-enabled <true|false>
  --idle-timeout-minutes <minutes> (default: 60)
  --desired-count <count>          (default: 0)
  --task-cpu <cpu>
  --task-memory <memory>
  --image-tag <tag>               (default: current UTC timestamp)
  --skip-image-build
  --s3-bucket <bucket>
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --allow-default-profile) ALLOW_DEFAULT_PROFILE="true"; shift 1 ;;
    --expected-account-id) EXPECTED_ACCOUNT_ID="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --environment-name) ENVIRONMENT_NAME="$2"; shift 2 ;;
    --python-stack-name) PYTHON_STACK_NAME="$2"; shift 2 ;;
    --r-stack-name) R_STACK_NAME="$2"; shift 2 ;;
    --catalog-bucket-name) CATALOG_BUCKET_NAME="$2"; shift 2 ;;
    --input-bucket-name) INPUT_BUCKET_NAME="$2"; shift 2 ;;
    --ephemeral-bucket-name) EPHEMERAL_BUCKET_NAME="$2"; shift 2 ;;
    --catalog-prefix) CATALOG_PREFIX="$2"; shift 2 ;;
    --ephemeral-prefix) EPHEMERAL_PREFIX="$2"; shift 2 ;;
    --allowed-ingress-cidr) ALLOWED_INGRESS_CIDR="$2"; shift 2 ;;
    --tls-certificate-arn) TLS_CERTIFICATE_ARN="$2"; shift 2 ;;
    --custom-domain-name) CUSTOM_DOMAIN_NAME="$2"; shift 2 ;;
    --custom-domain-hosted-zone-id) CUSTOM_DOMAIN_HOSTED_ZONE_ID="$2"; shift 2 ;;
    --runtime-enabled) RUNTIME_ENABLED="$2"; shift 2 ;;
    --idle-timeout-minutes) IDLE_TIMEOUT_MINUTES="$2"; shift 2 ;;
    --desired-count) DESIRED_COUNT="$2"; shift 2 ;;
    --task-cpu) TASK_CPU="$2"; shift 2 ;;
    --task-memory) TASK_MEMORY="$2"; shift 2 ;;
    --image-tag) IMAGE_TAG="$2"; shift 2 ;;
    --skip-image-build) SKIP_IMAGE_BUILD="true"; shift 1 ;;
    --s3-bucket) TEMPLATE_S3_BUCKET="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_PATH="${REPO_ROOT}/deployment/cfn/webapp-stack.yaml"

if [[ ! -f "${TEMPLATE_PATH}" ]]; then
  echo "Template not found: ${TEMPLATE_PATH}" >&2
  exit 1
fi
if [[ "${ALLOW_DEFAULT_PROFILE}" != "true" && -z "${PROFILE}" ]]; then
  echo "Guardrail: --profile is required unless --allow-default-profile is explicitly set." >&2
  exit 1
fi
if [[ "${RUNTIME_ENABLED}" != "true" && "${RUNTIME_ENABLED}" != "false" ]]; then
  echo "Guardrail: --runtime-enabled must be true or false." >&2
  exit 1
fi
if [[ -n "${CUSTOM_DOMAIN_NAME}" && -z "${CUSTOM_DOMAIN_HOSTED_ZONE_ID}" ]]; then
  echo "Guardrail: --custom-domain-hosted-zone-id is required when --custom-domain-name is set." >&2
  exit 1
fi
if [[ -z "${CUSTOM_DOMAIN_NAME}" && -n "${CUSTOM_DOMAIN_HOSTED_ZONE_ID}" ]]; then
  echo "Guardrail: --custom-domain-name is required when --custom-domain-hosted-zone-id is set." >&2
  exit 1
fi

AWS_ARGS=(--region "${REGION}")
if [[ -n "${PROFILE}" ]]; then
  AWS_ARGS+=(--profile "${PROFILE}")
fi

CALLER_ACCOUNT_ID="$(
  aws sts get-caller-identity --query "Account" --output text "${AWS_ARGS[@]}"
)"
if [[ -z "${CALLER_ACCOUNT_ID}" || "${CALLER_ACCOUNT_ID}" == "None" ]]; then
  echo "Guardrail: unable to resolve caller account from STS." >&2
  exit 1
fi
if [[ -n "${EXPECTED_ACCOUNT_ID}" && "${CALLER_ACCOUNT_ID}" != "${EXPECTED_ACCOUNT_ID}" ]]; then
  echo "Guardrail: account mismatch. expected=${EXPECTED_ACCOUNT_ID} actual=${CALLER_ACCOUNT_ID}" >&2
  exit 1
fi

DEFAULT_VPC_ID="$(
  aws ec2 describe-vpcs \
    --filters Name=isDefault,Values=true \
    --query "Vpcs[0].VpcId" \
    --output text \
    "${AWS_ARGS[@]}"
)"
DEFAULT_SUBNETS_RAW="$(
  aws ec2 describe-subnets \
    --filters Name=vpc-id,Values="${DEFAULT_VPC_ID}" Name=default-for-az,Values=true \
    --query "Subnets[].SubnetId" \
    --output text \
    "${AWS_ARGS[@]}"
)"
DEFAULT_SUBNETS_CSV="$(tr '\t' ',' <<<"${DEFAULT_SUBNETS_RAW}")"

stack_output() {
  local stack_name="$1"
  local output_key="$2"
  aws cloudformation describe-stacks \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='${output_key}'].OutputValue | [0]" \
    --output text \
    "${AWS_ARGS[@]}"
}

stack_exists() {
  aws cloudformation describe-stacks \
    --stack-name "$1" \
    --query "Stacks[0].StackId" \
    --output text \
    "${AWS_ARGS[@]}" >/dev/null 2>&1
}

if [[ -z "${CATALOG_BUCKET_NAME}" ]]; then
  CATALOG_BUCKET_NAME="$(stack_output "${PYTHON_STACK_NAME}" "EvidenceBucketName")"
fi
if [[ -z "${INPUT_BUCKET_NAME}" ]]; then
  INPUT_BUCKET_NAME="$(stack_output "${PYTHON_STACK_NAME}" "InputBucketName")"
fi
if [[ -z "${EPHEMERAL_BUCKET_NAME}" ]]; then
  EPHEMERAL_BUCKET_NAME="$(stack_output "${PYTHON_STACK_NAME}" "EphemeralBucketName")"
fi
if [[ -z "${INPUT_BUCKET_NAME}" || "${INPUT_BUCKET_NAME}" == "None" ]]; then
  echo "Could not resolve input bucket name." >&2
  exit 1
fi
if [[ -z "${CATALOG_BUCKET_NAME}" || "${CATALOG_BUCKET_NAME}" == "None" ]]; then
  echo "Could not resolve catalog/evidence bucket name." >&2
  exit 1
fi
if [[ -z "${EPHEMERAL_BUCKET_NAME}" || "${EPHEMERAL_BUCKET_NAME}" == "None" ]]; then
  echo "Could not resolve ephemeral bucket name." >&2
  exit 1
fi

deploy_stack() {
  local runtime_enabled="$1"
  local desired_count="$2"
  local image_uri="$3"
  local extra_args=()
  if [[ -n "${TEMPLATE_S3_BUCKET}" ]]; then
    extra_args+=(--s3-bucket "${TEMPLATE_S3_BUCKET}")
  fi
  aws cloudformation deploy \
    --stack-name "${STACK_NAME}" \
    --template-file "${TEMPLATE_PATH}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides \
      "EnvironmentName=${ENVIRONMENT_NAME}" \
      "VpcId=${DEFAULT_VPC_ID}" \
      "SubnetIds=${DEFAULT_SUBNETS_CSV}" \
      "AllowedIngressCidr=${ALLOWED_INGRESS_CIDR}" \
      "TlsCertificateArn=${TLS_CERTIFICATE_ARN}" \
      "CustomDomainName=${CUSTOM_DOMAIN_NAME}" \
      "CustomDomainHostedZoneId=${CUSTOM_DOMAIN_HOSTED_ZONE_ID}" \
      "RuntimeEnabled=${runtime_enabled}" \
      "IdleTimeoutMinutes=${IDLE_TIMEOUT_MINUTES}" \
      "InputBucketName=${INPUT_BUCKET_NAME}" \
      "CatalogBucketName=${CATALOG_BUCKET_NAME}" \
      "EphemeralBucketName=${EPHEMERAL_BUCKET_NAME}" \
      "CatalogPrefix=${CATALOG_PREFIX}" \
      "EphemeralPrefix=${EPHEMERAL_PREFIX}" \
      "PythonStackName=${PYTHON_STACK_NAME}" \
      "RStackName=${R_STACK_NAME}" \
      "DesiredCount=${desired_count}" \
      "TaskCpu=${TASK_CPU}" \
      "TaskMemory=${TASK_MEMORY}" \
      "WebappImageUri=${image_uri}" \
    "${extra_args[@]}" \
    "${AWS_ARGS[@]}"
}

if [[ -z "${IMAGE_TAG}" ]]; then
  IMAGE_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
fi

FINAL_DESIRED_COUNT="${DESIRED_COUNT}"
if [[ "${RUNTIME_ENABLED}" != "true" ]]; then
  FINAL_DESIRED_COUNT="0"
fi

REPOSITORY_URI=""
if stack_exists "${STACK_NAME}"; then
  REPOSITORY_URI="$(stack_output "${STACK_NAME}" "WebappRepositoryUri")"
fi

if [[ -z "${REPOSITORY_URI}" || "${REPOSITORY_URI}" == "None" ]]; then
  echo "Bootstrapping webapp infrastructure stack ${STACK_NAME} with desired count 0 ..."
  deploy_stack "false" 0 "public.ecr.aws/docker/library/python:3.12-slim"
  REPOSITORY_URI="$(stack_output "${STACK_NAME}" "WebappRepositoryUri")"
fi

if [[ -z "${REPOSITORY_URI}" || "${REPOSITORY_URI}" == "None" ]]; then
  echo "Could not resolve WebappRepositoryUri after infrastructure deployment." >&2
  exit 1
fi

if [[ "${SKIP_IMAGE_BUILD}" != "true" ]]; then
  build_args=(
    --region "${REGION}"
    --stack-name "${STACK_NAME}"
    --image-tag "${IMAGE_TAG}"
  )
  if [[ -n "${PROFILE}" ]]; then
    build_args+=(--profile "${PROFILE}")
  fi
  if [[ "${ALLOW_DEFAULT_PROFILE}" == "true" ]]; then
    build_args+=(--allow-default-profile)
  fi
  bash "${SCRIPT_DIR}/build-webapp-image.sh" "${build_args[@]}" >/dev/null
fi

echo "Deploying webapp baseline stack ${STACK_NAME} with runtime disabled ..."
deploy_stack "false" 0 "${REPOSITORY_URI}:${IMAGE_TAG}"

if [[ "${RUNTIME_ENABLED}" == "true" || "${FINAL_DESIRED_COUNT}" != "0" ]]; then
  echo "Deploying webapp final stack ${STACK_NAME} runtime_enabled=${RUNTIME_ENABLED} desired_count=${FINAL_DESIRED_COUNT} ..."
  deploy_stack "${RUNTIME_ENABLED}" "${FINAL_DESIRED_COUNT}" "${REPOSITORY_URI}:${IMAGE_TAG}"
fi

aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
  --output table \
  "${AWS_ARGS[@]}"
