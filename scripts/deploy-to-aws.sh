#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# Configuration (edit these to match your environment before running)
# -----------------------------------------------------------------------------
PROFILE="AdministratorAccess-807497180525"
REGION="us-east-1"
STACK_NAME="package-scanner-dev"
ENVIRONMENT_NAME="package-scanner-dev"
INPUT_BUCKET="package-scanner-dev-scan-input-807497180525-us-east-1"
TEMPLATE_S3_BUCKET="${INPUT_BUCKET}"
DEPLOYMENT_LOCK_TOKEN="dev-lock-20260306"
EXPECTED_ACCOUNT_ID="807497180525"
PY_ENV_FILE="./.tmp-tests/smoke-python-environment.yml"
PY_INPUT_S3_KEY="package-scanner/inputs/python/smoke-python-environment.yml"  # S3 object key (remote path)
R_ENV_FILE="/home/tjansto/github/natgo/package-scanner/.tmp-tests/smoke-renv.lock"
R_INPUT_KEY="inputs/r/smoke-renv.lock"
SAFETY_API_KEY="8c73d240-2e0d3f4a-f9544793-670187fa"

# -----------------------------------------------------------------------------

require_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "Required file not found: ${path}" >&2
    exit 1
  fi
}

run() {
  echo "+ $*"
  "$@"
}

echo "==> Validating local inputs"
require_file "${PY_ENV_FILE}"
require_file "${R_ENV_FILE}"

echo "==> Deploying/refreshing CloudFormation stack"
run ./scripts/deploy-cfn.sh \
  --region "${REGION}" \
  --profile "${PROFILE}" \
  --expected-account-id "${EXPECTED_ACCOUNT_ID}" \
  --deployment-lock-token "${DEPLOYMENT_LOCK_TOKEN}" \
  --stack-name "${STACK_NAME}" \
  --environment-name "${ENVIRONMENT_NAME}" \
  --s3-bucket "${TEMPLATE_S3_BUCKET}"

echo "==> Uploading Python environment to s3://${INPUT_BUCKET}/${PY_INPUT_S3_KEY}"
run aws s3 cp "${PY_ENV_FILE}" \
  "s3://${INPUT_BUCKET}/${PY_INPUT_S3_KEY}" \
  --region "${REGION}" \
  --profile "${PROFILE}"

echo "==> Uploading R lockfile to s3://${INPUT_BUCKET}/${R_INPUT_KEY}"
run aws s3 cp "${R_ENV_FILE}" \
  "s3://${INPUT_BUCKET}/${R_INPUT_KEY}" \
  --region "${REGION}" \
  --profile "${PROFILE}"

echo "==> Starting Python (numpy) smoke scan"
run ./scripts/start-python-scan.sh \
  --stack-name "${STACK_NAME}" \
  --input-bucket "${INPUT_BUCKET}" \
  --input-object-key "${PY_INPUT_S3_KEY}" \
  --region "${REGION}" \
  --profile "${PROFILE}" \
  --expected-account-id "${EXPECTED_ACCOUNT_ID}" \
  --deployment-lock-token "${DEPLOYMENT_LOCK_TOKEN}" \
  --remediate-medium true \
  --fail-on-medium false \
  --safety-api-key "${SAFETY_API_KEY}"

echo "==> Starting R (tidyverse) smoke scan"
run ./scripts/start-r-scan.sh \
  --stack-name "${STACK_NAME}" \
  --input-bucket "${INPUT_BUCKET}" \
  --input-object-key "${R_INPUT_KEY}" \
  --region "${REGION}" \
  --profile "${PROFILE}" \
  --expected-account-id "${EXPECTED_ACCOUNT_ID}" \
  --deployment-lock-token "${DEPLOYMENT_LOCK_TOKEN}" \
  --remediate-medium true \
  --fail-on-medium false

echo "All AWS actions submitted. Monitor CodeBuild for completion."
