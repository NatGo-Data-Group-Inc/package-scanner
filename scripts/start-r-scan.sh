#!/usr/bin/env bash
set -euo pipefail

STACK_NAME=""
INPUT_BUCKET=""
INPUT_OBJECT_KEY="inputs/r/renv.lock"
EVIDENCE_BUCKET=""
EVIDENCE_PREFIX="evidence"
EPHEMERAL_BUCKET=""
EPHEMERAL_PREFIX="deploy/tmp/r"
REGION="us-east-1"
PROFILE=""
ALLOW_DEFAULT_PROFILE="false"
EXPECTED_ACCOUNT_ID=""
DEPLOYMENT_LOCK_TOKEN=""
REMEDIATE_MEDIUM="true"
FAIL_ON_MEDIUM="false"

usage() {
  cat <<'EOF'
Usage: start-r-scan.sh --stack-name <name> --input-bucket <bucket> [options]

Required:
  --stack-name <name>
  --input-bucket <bucket>

Optional:
  --input-object-key <key>                (default: inputs/r/renv.lock)
  --evidence-bucket <bucket>              (default: auto from stack output)
  --evidence-prefix <prefix>              (default: evidence)
  --ephemeral-bucket <bucket>             (default: auto from stack output)
  --ephemeral-prefix <prefix>             (default: deploy/tmp/r)
  --region <region>                       (default: us-east-1)
  --profile <profile>
  --allow-default-profile
  --expected-account-id <account-id>
  --deployment-lock-token <token>         (required)
  --remediate-medium <true|false>         (default: true)
  --fail-on-medium <true|false>           (default: false)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --input-bucket) INPUT_BUCKET="$2"; shift 2 ;;
    --input-object-key) INPUT_OBJECT_KEY="$2"; shift 2 ;;
    --evidence-bucket) EVIDENCE_BUCKET="$2"; shift 2 ;;
    --evidence-prefix) EVIDENCE_PREFIX="$2"; shift 2 ;;
    --ephemeral-bucket) EPHEMERAL_BUCKET="$2"; shift 2 ;;
    --ephemeral-prefix) EPHEMERAL_PREFIX="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --allow-default-profile) ALLOW_DEFAULT_PROFILE="true"; shift 1 ;;
    --expected-account-id) EXPECTED_ACCOUNT_ID="$2"; shift 2 ;;
    --deployment-lock-token) DEPLOYMENT_LOCK_TOKEN="$2"; shift 2 ;;
    --remediate-medium) REMEDIATE_MEDIUM="$2"; shift 2 ;;
    --fail-on-medium) FAIL_ON_MEDIUM="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${STACK_NAME}" || -z "${INPUT_BUCKET}" ]]; then
  echo "Both --stack-name and --input-bucket are required." >&2
  usage >&2
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

STACK_LOCK_TOKEN="$(
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Tags[?Key=='DeploymentLockToken'].Value | [0]" \
    --output text \
    "${AWS_ARGS[@]}"
)"
if [[ -z "${STACK_LOCK_TOKEN}" || "${STACK_LOCK_TOKEN}" == "None" ]]; then
  echo "Guardrail: stack ${STACK_NAME} is not lock-tagged (DeploymentLockToken)." >&2
  exit 1
fi
if [[ "${STACK_LOCK_TOKEN}" != "${DEPLOYMENT_LOCK_TOKEN}" ]]; then
  echo "Guardrail: deployment lock token mismatch for stack ${STACK_NAME}." >&2
  exit 1
fi

stack_output() {
  local key="$1"
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue | [0]" \
    --output text \
    "${AWS_ARGS[@]}"
}

if [[ -z "${EVIDENCE_BUCKET}" ]]; then
  EVIDENCE_BUCKET="$(stack_output EvidenceBucketName)"
fi
if [[ -z "${EPHEMERAL_BUCKET}" ]]; then
  EPHEMERAL_BUCKET="$(stack_output EphemeralBucketName)"
fi
if [[ -z "${EVIDENCE_BUCKET}" || "${EVIDENCE_BUCKET}" == "None" ]]; then
  echo "EvidenceBucket output not found." >&2
  exit 1
fi
if [[ -z "${EPHEMERAL_BUCKET}" || "${EPHEMERAL_BUCKET}" == "None" ]]; then
  echo "EphemeralBucket output not found." >&2
  exit 1
fi

start_build() {
  local platform="$1"
  local output_key="$2"
  local project_name
  local build_id

  project_name="$(stack_output "${output_key}")"
  if [[ -z "${project_name}" || "${project_name}" == "None" ]]; then
    echo "Missing stack output: ${output_key}" >&2
    exit 1
  fi

  echo "Starting R ${platform} scan: ${project_name}"
  build_id="$(
    aws codebuild start-build \
      --project-name "${project_name}" \
      --environment-variables-override \
        "name=INPUT_BUCKET,value=${INPUT_BUCKET},type=PLAINTEXT" \
        "name=INPUT_OBJECT_KEY,value=${INPUT_OBJECT_KEY},type=PLAINTEXT" \
        "name=EVIDENCE_BUCKET,value=${EVIDENCE_BUCKET},type=PLAINTEXT" \
        "name=EVIDENCE_PREFIX,value=${EVIDENCE_PREFIX},type=PLAINTEXT" \
        "name=EPHEMERAL_BUCKET,value=${EPHEMERAL_BUCKET},type=PLAINTEXT" \
        "name=EPHEMERAL_PREFIX,value=${EPHEMERAL_PREFIX},type=PLAINTEXT" \
        "name=REMEDIATE_MEDIUM,value=${REMEDIATE_MEDIUM},type=PLAINTEXT" \
        "name=FAIL_ON_MEDIUM,value=${FAIL_ON_MEDIUM},type=PLAINTEXT" \
      --query "build.id" \
      --output text \
      "${AWS_ARGS[@]}"
  )"

  printf '%-16s %-48s %s\n' "${platform}" "${project_name}" "${build_id}"
}

printf '%-16s %-48s %s\n' "Platform" "ProjectName" "BuildId"
start_build "linux-amd64" "RLinuxAmd64ProjectName"
start_build "linux-arm64" "RLinuxArm64ProjectName"
start_build "windows-amd64" "RWindowsAmd64ProjectName"
