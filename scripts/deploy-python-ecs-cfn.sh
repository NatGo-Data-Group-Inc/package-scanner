#!/usr/bin/env bash
set -euo pipefail

REGION="us-east-1"
PROFILE=""
ALLOW_DEFAULT_PROFILE="false"
EXPECTED_ACCOUNT_ID=""
STACK_NAME="package-scanner-python-ecs-dev"
ENVIRONMENT_NAME="package-scanner-dev"
DEPLOYMENT_LOCK_TOKEN=""
EXISTING_INPUT_BUCKET_NAME=""
EXISTING_EVIDENCE_BUCKET_NAME=""
EXISTING_EPHEMERAL_BUCKET_NAME=""
LINUX_AMD64_INSTANCE_TYPE="m7i.xlarge"
LINUX_ARM64_INSTANCE_TYPE="m7g.xlarge"
LINUX_ROOT_VOLUME_SIZE="250"
WINDOWS_AMD64_INSTANCE_TYPE="m7i.xlarge"
WINDOWS_ROOT_VOLUME_SIZE="300"
TEMPLATE_S3_BUCKET=""

usage() {
  cat <<'EOF'
Usage: deploy-python-ecs-cfn.sh [options]

Options:
  --region <region>
  --profile <profile>
  --allow-default-profile
  --expected-account-id <account-id>
  --stack-name <name>
  --environment-name <name>
  --deployment-lock-token <token>
  --existing-input-bucket-name <bucket>
  --existing-evidence-bucket-name <bucket>
  --existing-ephemeral-bucket-name <bucket>
  --linux-amd64-instance-type <type>
  --linux-arm64-instance-type <type>
  --linux-root-volume-size <gb>
  --windows-amd64-instance-type <type>
  --windows-root-volume-size <gb>
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
    --deployment-lock-token) DEPLOYMENT_LOCK_TOKEN="$2"; shift 2 ;;
    --existing-input-bucket-name) EXISTING_INPUT_BUCKET_NAME="$2"; shift 2 ;;
    --existing-evidence-bucket-name) EXISTING_EVIDENCE_BUCKET_NAME="$2"; shift 2 ;;
    --existing-ephemeral-bucket-name) EXISTING_EPHEMERAL_BUCKET_NAME="$2"; shift 2 ;;
    --linux-amd64-instance-type) LINUX_AMD64_INSTANCE_TYPE="$2"; shift 2 ;;
    --linux-arm64-instance-type) LINUX_ARM64_INSTANCE_TYPE="$2"; shift 2 ;;
    --linux-root-volume-size) LINUX_ROOT_VOLUME_SIZE="$2"; shift 2 ;;
    --windows-amd64-instance-type) WINDOWS_AMD64_INSTANCE_TYPE="$2"; shift 2 ;;
    --windows-root-volume-size) WINDOWS_ROOT_VOLUME_SIZE="$2"; shift 2 ;;
    --s3-bucket) TEMPLATE_S3_BUCKET="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_PATH="${REPO_ROOT}/deployment/cfn/python-ecs-scan-stack.yaml"

if [[ ! -f "${TEMPLATE_PATH}" ]]; then
  echo "Template not found: ${TEMPLATE_PATH}" >&2
  exit 1
fi
if [[ "${ALLOW_DEFAULT_PROFILE}" != "true" && -z "${PROFILE}" ]]; then
  echo "Guardrail: --profile is required unless --allow-default-profile is explicitly set." >&2
  exit 1
fi
if [[ -z "${DEPLOYMENT_LOCK_TOKEN}" ]]; then
  echo "Guardrail: --deployment-lock-token is required." >&2
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
if [[ -z "${DEFAULT_VPC_ID}" || "${DEFAULT_VPC_ID}" == "None" ]]; then
  echo "Could not resolve default VPC." >&2
  exit 1
fi

DEFAULT_SUBNETS_RAW="$(
  aws ec2 describe-subnets \
    --filters Name=vpc-id,Values="${DEFAULT_VPC_ID}" Name=default-for-az,Values=true \
    --query "Subnets[].SubnetId" \
    --output text \
    "${AWS_ARGS[@]}"
)"
if [[ -z "${DEFAULT_SUBNETS_RAW}" || "${DEFAULT_SUBNETS_RAW}" == "None" ]]; then
  echo "Could not resolve default subnets for VPC ${DEFAULT_VPC_ID}." >&2
  exit 1
fi
DEFAULT_SUBNETS_CSV="$(tr '\t' ',' <<<"${DEFAULT_SUBNETS_RAW}")"

PARAM_OVERRIDES=(
  "EnvironmentName=${ENVIRONMENT_NAME}"
  "VpcId=${DEFAULT_VPC_ID}"
  "LinuxSubnetIds=${DEFAULT_SUBNETS_CSV}"
  "WindowsSubnetIds=${DEFAULT_SUBNETS_CSV}"
  "LinuxAmd64InstanceType=${LINUX_AMD64_INSTANCE_TYPE}"
  "LinuxArm64InstanceType=${LINUX_ARM64_INSTANCE_TYPE}"
  "LinuxRootVolumeSize=${LINUX_ROOT_VOLUME_SIZE}"
  "WindowsAmd64InstanceType=${WINDOWS_AMD64_INSTANCE_TYPE}"
  "WindowsRootVolumeSize=${WINDOWS_ROOT_VOLUME_SIZE}"
)

if [[ -n "${EXISTING_INPUT_BUCKET_NAME}" ]]; then
  PARAM_OVERRIDES+=("ExistingInputBucketName=${EXISTING_INPUT_BUCKET_NAME}")
fi
if [[ -n "${EXISTING_EVIDENCE_BUCKET_NAME}" ]]; then
  PARAM_OVERRIDES+=("ExistingEvidenceBucketName=${EXISTING_EVIDENCE_BUCKET_NAME}")
fi
if [[ -n "${EXISTING_EPHEMERAL_BUCKET_NAME}" ]]; then
  PARAM_OVERRIDES+=("ExistingEphemeralBucketName=${EXISTING_EPHEMERAL_BUCKET_NAME}")
fi

STACK_LOCK_TOKEN="$(
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Tags[?Key=='DeploymentLockToken'].Value | [0]" \
    --output text \
    "${AWS_ARGS[@]}" 2>/dev/null || true
)"
if [[ -n "${STACK_LOCK_TOKEN}" && "${STACK_LOCK_TOKEN}" != "None" && "${STACK_LOCK_TOKEN}" != "${DEPLOYMENT_LOCK_TOKEN}" ]]; then
  echo "Guardrail: deployment lock token mismatch for stack ${STACK_NAME}." >&2
  exit 1
fi

EXTRA_DEPLOY_ARGS=()
if [[ -n "${TEMPLATE_S3_BUCKET}" ]]; then
  EXTRA_DEPLOY_ARGS+=(--s3-bucket "${TEMPLATE_S3_BUCKET}")
fi

echo "Deploying Python ECS stack ${STACK_NAME} in ${REGION} ..."
aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE_PATH}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --tags \
    "DeploymentLockToken=${DEPLOYMENT_LOCK_TOKEN}" \
    "EnvironmentName=${ENVIRONMENT_NAME}" \
    "Project=package_scanner" \
    "Purpose=cyber-package-scanning-python-ecs-worker-plane" \
    "ManagedBy=cloudformation" \
  --parameter-overrides "${PARAM_OVERRIDES[@]}" \
  "${EXTRA_DEPLOY_ARGS[@]}" \
  "${AWS_ARGS[@]}"

echo "Fetching outputs ..."
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
  --output table \
  "${AWS_ARGS[@]}"
