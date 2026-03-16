#!/usr/bin/env bash
set -euo pipefail

REGION="us-east-1"
PROFILE=""
ALLOW_DEFAULT_PROFILE="false"
EXPECTED_ACCOUNT_ID=""
STACK_NAME="package-scanner-dev"
ENVIRONMENT_NAME="package-scanner-dev"
DEPLOYMENT_LOCK_TOKEN=""
EXISTING_INPUT_BUCKET_NAME=""
EXISTING_EVIDENCE_BUCKET_NAME=""
EXISTING_EPHEMERAL_BUCKET_NAME=""
BUILD_TIMEOUT_MINUTES="90"
LINUX_COMPUTE_TYPE="BUILD_GENERAL1_MEDIUM"
LINUX_ARM_COMPUTE_TYPE="BUILD_GENERAL1_LARGE"
WINDOWS_COMPUTE_TYPE="BUILD_GENERAL1_LARGE"
TRIVY_VERSION="0.69.3"
TRIVY_RELEASE_BASE_URL="https://github.com/aquasecurity/trivy/releases/download"
TEMPLATE_S3_BUCKET=""

usage() {
  cat <<'EOF'
Usage: deploy-cfn.sh [options]

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
  --build-timeout-minutes <minutes>
  --linux-compute-type <type>
  --linux-arm-compute-type <type>
  --windows-compute-type <type>
  --trivy-version <version>
  --trivy-release-base-url <url>
  --s3-bucket <bucket>                       # optional: bucket for large templates
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
    --build-timeout-minutes) BUILD_TIMEOUT_MINUTES="$2"; shift 2 ;;
    --linux-compute-type) LINUX_COMPUTE_TYPE="$2"; shift 2 ;;
    --linux-arm-compute-type) LINUX_ARM_COMPUTE_TYPE="$2"; shift 2 ;;
    --windows-compute-type) WINDOWS_COMPUTE_TYPE="$2"; shift 2 ;;
    --trivy-version) TRIVY_VERSION="$2"; shift 2 ;;
    --trivy-release-base-url) TRIVY_RELEASE_BASE_URL="$2"; shift 2 ;;
    --s3-bucket) TEMPLATE_S3_BUCKET="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_PATH="${REPO_ROOT}/deployment/cfn/python-scan-stack.yaml"

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
  aws sts get-caller-identity \
    --query "Account" \
    --output text \
    "${AWS_ARGS[@]}"
)"
if [[ -z "${CALLER_ACCOUNT_ID}" || "${CALLER_ACCOUNT_ID}" == "None" ]]; then
  echo "Guardrail: unable to resolve caller account from STS." >&2
  exit 1
fi
if [[ -n "${EXPECTED_ACCOUNT_ID}" && "${CALLER_ACCOUNT_ID}" != "${EXPECTED_ACCOUNT_ID}" ]]; then
  echo "Guardrail: account mismatch. expected=${EXPECTED_ACCOUNT_ID} actual=${CALLER_ACCOUNT_ID}" >&2
  exit 1
fi

PARAM_OVERRIDES=(
  "EnvironmentName=${ENVIRONMENT_NAME}"
  "BuildTimeoutMinutes=${BUILD_TIMEOUT_MINUTES}"
  "LinuxComputeType=${LINUX_COMPUTE_TYPE}"
  "LinuxArmComputeType=${LINUX_ARM_COMPUTE_TYPE}"
  "WindowsComputeType=${WINDOWS_COMPUTE_TYPE}"
  "TrivyVersion=${TRIVY_VERSION}"
  "TrivyReleaseBaseUrl=${TRIVY_RELEASE_BASE_URL}"
)

if command -v sha256sum >/dev/null 2>&1; then
  GOVERNANCE_SCRIPT_SHA256="$(sha256sum "${REPO_ROOT}/scripts/generate-governance-artifacts.py" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
  GOVERNANCE_SCRIPT_SHA256="$(shasum -a 256 "${REPO_ROOT}/scripts/generate-governance-artifacts.py" | awk '{print $1}')"
else
  echo "Neither sha256sum nor shasum was found; cannot compute governance script hash." >&2
  exit 1
fi
PARAM_OVERRIDES+=("GovernanceScriptSha256=${GOVERNANCE_SCRIPT_SHA256}")

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
  echo "Expected existing token: ${STACK_LOCK_TOKEN}" >&2
  exit 1
fi

EXTRA_DEPLOY_ARGS=()
if [[ -n "${TEMPLATE_S3_BUCKET}" ]]; then
  EXTRA_DEPLOY_ARGS+=(--s3-bucket "${TEMPLATE_S3_BUCKET}")
fi

echo "Deploying stack ${STACK_NAME} in ${REGION} ..."
aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE_PATH}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --tags \
    "DeploymentLockToken=${DEPLOYMENT_LOCK_TOKEN}" \
    "EnvironmentName=${ENVIRONMENT_NAME}" \
    "Project=package_scanner" \
    "Purpose=cyber-package-scanning-control-and-data-plane" \
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

EVIDENCE_BUCKET="$(
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='EvidenceBucketName'].OutputValue | [0]" \
    --output text \
    "${AWS_ARGS[@]}"
)"

if [[ -z "${EVIDENCE_BUCKET}" || "${EVIDENCE_BUCKET}" == "None" ]]; then
  echo "Could not resolve EvidenceBucketName output for governance script upload." >&2
  exit 1
fi

echo "Uploading governance artifact generator to s3://${EVIDENCE_BUCKET}/config/generate-governance-artifacts.py ..."
aws s3 cp "${REPO_ROOT}/scripts/generate-governance-artifacts.py" \
  "s3://${EVIDENCE_BUCKET}/config/generate-governance-artifacts.py" \
  "${AWS_ARGS[@]}"

echo "Uploading R governance and SBOM generators ..."
aws s3 cp "${REPO_ROOT}/scripts/generate-r-governance-artifacts.py" \
  "s3://${EVIDENCE_BUCKET}/config/generate-r-governance-artifacts.py" \
  "${AWS_ARGS[@]}"
aws s3 cp "${REPO_ROOT}/scripts/generate-r-sbom.py" \
  "s3://${EVIDENCE_BUCKET}/config/generate-r-sbom.py" \
  "${AWS_ARGS[@]}"
aws s3 cp "${REPO_ROOT}/scripts/materialize-r-environment.R" \
  "s3://${EVIDENCE_BUCKET}/config/materialize-r-environment.R" \
  "${AWS_ARGS[@]}"
aws s3 cp "${REPO_ROOT}/scripts/install-r-runtime.sh" \
  "s3://${EVIDENCE_BUCKET}/config/install-r-runtime.sh" \
  "${AWS_ARGS[@]}"
aws s3 cp "${REPO_ROOT}/scripts/install-r-runtime.ps1" \
  "s3://${EVIDENCE_BUCKET}/config/install-r-runtime.ps1" \
  "${AWS_ARGS[@]}"
aws s3 cp "${REPO_ROOT}/scripts/bundle-directory.py" \
  "s3://${EVIDENCE_BUCKET}/config/bundle-directory.py" \
  "${AWS_ARGS[@]}"
aws s3 cp "${REPO_ROOT}/scripts/generate-r-materialization-summary.py" \
  "s3://${EVIDENCE_BUCKET}/config/generate-r-materialization-summary.py" \
  "${AWS_ARGS[@]}"
