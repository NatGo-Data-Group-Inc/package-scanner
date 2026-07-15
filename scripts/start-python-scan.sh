#!/usr/bin/env bash
set -euo pipefail

STACK_NAME=""
INPUT_BUCKET=""
INPUT_OBJECT_KEY="inputs/python/environment.yml"
SOURCE_ENVIRONMENT_FILE=""
EVIDENCE_BUCKET=""
EVIDENCE_PREFIX="evidence"
EPHEMERAL_BUCKET=""
EPHEMERAL_PREFIX="deploy/tmp/python"
REGION="us-east-1"
PROFILE=""
ALLOW_DEFAULT_PROFILE="false"
EXPECTED_ACCOUNT_ID=""
DEPLOYMENT_LOCK_TOKEN=""
ENABLE_FORTIFY="false"
FORTIFY_COMMAND=""
REMEDIATE_MEDIUM="true"
FAIL_ON_MEDIUM="false"
SAFETY_API_KEY=""
PLATFORM_SET="all"

usage() {
  cat <<'EOF'
Usage: start-python-scan.sh --stack-name <name> --input-bucket <bucket> [options]

Required:
  --stack-name <name>
  --input-bucket <bucket>

Optional:
  --input-object-key <key>                (default: inputs/python/environment.yml)
  --source-environment-file <path>        (default: no upload; use existing S3 object)
  --evidence-bucket <bucket>              (default: auto from stack output)
  --evidence-prefix <prefix>              (default: evidence)
  --ephemeral-bucket <bucket>             (default: auto from stack output)
  --ephemeral-prefix <prefix>             (default: deploy/tmp/python)
  --region <region>                       (default: us-east-1)
  --profile <profile>
  --allow-default-profile
  --expected-account-id <account-id>
  --deployment-lock-token <token>         (required)
  --enable-fortify
  --fortify-command <command>
  --remediate-medium <true|false>         (default: true)
  --fail-on-medium <true|false>           (default: false)
  --safety-api-key <key>                  (default: empty/unauthenticated)
  --platform-set <all|linux-only|linux-amd64|linux-arm64|windows-only>
                                           (default: all)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --input-bucket) INPUT_BUCKET="$2"; shift 2 ;;
    --input-object-key) INPUT_OBJECT_KEY="$2"; shift 2 ;;
    --source-environment-file) SOURCE_ENVIRONMENT_FILE="$2"; shift 2 ;;
    --evidence-bucket) EVIDENCE_BUCKET="$2"; shift 2 ;;
    --evidence-prefix) EVIDENCE_PREFIX="$2"; shift 2 ;;
    --ephemeral-bucket) EPHEMERAL_BUCKET="$2"; shift 2 ;;
    --ephemeral-prefix) EPHEMERAL_PREFIX="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --allow-default-profile) ALLOW_DEFAULT_PROFILE="true"; shift 1 ;;
    --expected-account-id) EXPECTED_ACCOUNT_ID="$2"; shift 2 ;;
    --deployment-lock-token) DEPLOYMENT_LOCK_TOKEN="$2"; shift 2 ;;
    --enable-fortify) ENABLE_FORTIFY="true"; shift 1 ;;
    --fortify-command) FORTIFY_COMMAND="$2"; shift 2 ;;
    --remediate-medium) REMEDIATE_MEDIUM="$2"; shift 2 ;;
    --fail-on-medium) FAIL_ON_MEDIUM="$2"; shift 2 ;;
    --safety-api-key) SAFETY_API_KEY="$2"; shift 2 ;;
    --platform-set) PLATFORM_SET="$2"; shift 2 ;;
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
if [[ "${PLATFORM_SET}" != "all" && "${PLATFORM_SET}" != "linux-only" && "${PLATFORM_SET}" != "linux-amd64" && "${PLATFORM_SET}" != "linux-arm64" && "${PLATFORM_SET}" != "windows-only" ]]; then
  echo "Guardrail: --platform-set must be one of: all, linux-only, linux-amd64, linux-arm64, windows-only" >&2
  exit 1
fi
if [[ -n "${SOURCE_ENVIRONMENT_FILE}" && ! -f "${SOURCE_ENVIRONMENT_FILE}" ]]; then
  echo "Source environment file not found: ${SOURCE_ENVIRONMENT_FILE}" >&2
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
  echo "Expected existing token: ${STACK_LOCK_TOKEN}" >&2
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

cluster_name_from_arn() {
  local cluster_arn="$1"
  printf '%s\n' "${cluster_arn##*/}"
}

default_asg_name_for_cluster() {
  local cluster_name="$1"
  printf '%s\n' "${cluster_name}"
}

ensure_worker_capacity() {
  local cluster_arn="$1"
  local asg_output_key="$2"
  local cluster_name asg_name asg_json min_size desired_capacity active_count attempts

  cluster_name="$(cluster_name_from_arn "${cluster_arn}")"
  asg_name="$(stack_output "${asg_output_key}")"
  if [[ -z "${asg_name}" || "${asg_name}" == "None" ]]; then
    asg_name="$(default_asg_name_for_cluster "${cluster_name}")"
  fi
  if [[ -z "${asg_name}" ]]; then
    echo "Unable to resolve Auto Scaling group for cluster ${cluster_name}" >&2
    exit 1
  fi

  asg_json="$(
    aws autoscaling describe-auto-scaling-groups \
      --auto-scaling-group-names "${asg_name}" \
      --query 'AutoScalingGroups[0].{MinSize:MinSize,DesiredCapacity:DesiredCapacity}' \
      --output json \
      "${AWS_ARGS[@]}"
  )"
  if [[ -z "${asg_json}" || "${asg_json}" == "null" ]]; then
    echo "Unable to describe Auto Scaling group ${asg_name} for cluster ${cluster_name}" >&2
    exit 1
  fi

  min_size="$(
    python -c 'import json,sys; data=json.load(sys.stdin); print(data.get("MinSize", ""))' <<<"${asg_json}"
  )"
  desired_capacity="$(
    python -c 'import json,sys; data=json.load(sys.stdin); print(data.get("DesiredCapacity", ""))' <<<"${asg_json}"
  )"

  if [[ -z "${min_size}" || -z "${desired_capacity}" ]]; then
    echo "Incomplete Auto Scaling data for ${asg_name}" >&2
    exit 1
  fi

  if (( min_size < 1 || desired_capacity < 1 )); then
    echo "Scaling ${asg_name} for ${cluster_name} to minimum worker capacity (min=1 desired=1)"
    aws autoscaling update-auto-scaling-group \
      --auto-scaling-group-name "${asg_name}" \
      --min-size 1 \
      --desired-capacity 1 \
      "${AWS_ARGS[@]}" >/dev/null
  fi

  echo "Waiting for an ACTIVE ECS container instance in ${cluster_name}"
  for attempts in $(seq 1 40); do
    active_count="$(
      aws ecs list-container-instances \
        --cluster "${cluster_name}" \
        --status ACTIVE \
        --query 'length(containerInstanceArns)' \
        --output text \
        "${AWS_ARGS[@]}"
    )"
    if [[ "${active_count}" != "None" && "${active_count}" =~ ^[0-9]+$ ]] && (( active_count > 0 )); then
      echo "Cluster ${cluster_name} has ${active_count} active container instance(s)"
      return
    fi
    sleep 15
  done

  echo "Timed out waiting for ACTIVE ECS capacity in cluster ${cluster_name}" >&2
  exit 1
}

if [[ -n "${SOURCE_ENVIRONMENT_FILE}" ]]; then
  echo "Uploading ${SOURCE_ENVIRONMENT_FILE} to s3://${INPUT_BUCKET}/${INPUT_OBJECT_KEY}"
  aws s3 cp "${SOURCE_ENVIRONMENT_FILE}" "s3://${INPUT_BUCKET}/${INPUT_OBJECT_KEY}" "${AWS_ARGS[@]}" >/dev/null
fi

if [[ -z "${EVIDENCE_BUCKET}" ]]; then
  EVIDENCE_BUCKET="$(stack_output EvidenceBucketName)"
  if [[ -z "${EVIDENCE_BUCKET}" || "${EVIDENCE_BUCKET}" == "None" ]]; then
    echo "EvidenceBucket was not provided and EvidenceBucketName output was not found." >&2
    exit 1
  fi
fi

if [[ -z "${EPHEMERAL_BUCKET}" ]]; then
  EPHEMERAL_BUCKET="$(stack_output EphemeralBucketName)"
  if [[ -z "${EPHEMERAL_BUCKET}" || "${EPHEMERAL_BUCKET}" == "None" ]]; then
    echo "EphemeralBucket was not provided and EphemeralBucketName output was not found." >&2
    exit 1
  fi
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

  echo "Starting ${platform}: ${project_name}"
  build_id="$(aws codebuild start-build \
    --project-name "${project_name}" \
    --environment-variables-override \
      "name=INPUT_BUCKET,value=${INPUT_BUCKET},type=PLAINTEXT" \
      "name=INPUT_OBJECT_KEY,value=${INPUT_OBJECT_KEY},type=PLAINTEXT" \
      "name=EVIDENCE_BUCKET,value=${EVIDENCE_BUCKET},type=PLAINTEXT" \
      "name=EVIDENCE_PREFIX,value=${EVIDENCE_PREFIX},type=PLAINTEXT" \
      "name=EPHEMERAL_BUCKET,value=${EPHEMERAL_BUCKET},type=PLAINTEXT" \
      "name=EPHEMERAL_PREFIX,value=${EPHEMERAL_PREFIX},type=PLAINTEXT" \
      "name=ENABLE_FORTIFY,value=${ENABLE_FORTIFY},type=PLAINTEXT" \
      "name=FORTIFY_COMMAND,value=${FORTIFY_COMMAND},type=PLAINTEXT" \
      "name=REMEDIATE_MEDIUM,value=${REMEDIATE_MEDIUM},type=PLAINTEXT" \
      "name=FAIL_ON_MEDIUM,value=${FAIL_ON_MEDIUM},type=PLAINTEXT" \
      "name=SAFETY_API_KEY,value=${SAFETY_API_KEY},type=PLAINTEXT" \
    --query "build.id" \
    --output text \
    "${AWS_ARGS[@]}")"

  printf '%-16s %-48s %s\n' "${platform}" "${project_name}" "${build_id}"
}

start_ecs_scan() {
  local state_machine_arn execution_name execution_arn timestamp
  local platforms_json

  state_machine_arn="$(stack_output PythonScanOrchestrationStateMachineArn)"
  if [[ -z "${state_machine_arn}" || "${state_machine_arn}" == "None" ]]; then
    state_machine_arn="$(stack_output PythonLinuxScanOrchestrationStateMachineArn)"
  fi
  if [[ -z "${state_machine_arn}" || "${state_machine_arn}" == "None" ]]; then
    echo "Missing stack output: PythonScanOrchestrationStateMachineArn" >&2
    exit 1
  fi

  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  execution_name="python-scan-${timestamp}-$(openssl rand -hex 4)"
  case "${PLATFORM_SET}" in
    linux-amd64) platforms_json='["linux-amd64"]' ;;
    linux-arm64) platforms_json='["linux-arm64"]' ;;
    windows-only) platforms_json='["windows-amd64"]' ;;
    linux-only) platforms_json='["linux-amd64","linux-arm64"]' ;;
    *) platforms_json='["linux-amd64","linux-arm64","windows-amd64"]' ;;
  esac

  case "${PLATFORM_SET}" in
    linux-amd64)
      ensure_worker_capacity "$(stack_output PythonLinuxAmd64ClusterArn)" "PythonLinuxAmd64AutoScalingGroupName"
      ;;
    linux-arm64)
      ensure_worker_capacity "$(stack_output PythonLinuxArm64ClusterArn)" "PythonLinuxArm64AutoScalingGroupName"
      ;;
    windows-only)
      ensure_worker_capacity "$(stack_output PythonWindowsAmd64ClusterArn)" "PythonWindowsAmd64AutoScalingGroupName"
      ;;
    linux-only)
      ensure_worker_capacity "$(stack_output PythonLinuxAmd64ClusterArn)" "PythonLinuxAmd64AutoScalingGroupName"
      ensure_worker_capacity "$(stack_output PythonLinuxArm64ClusterArn)" "PythonLinuxArm64AutoScalingGroupName"
      ;;
    *)
      ensure_worker_capacity "$(stack_output PythonLinuxAmd64ClusterArn)" "PythonLinuxAmd64AutoScalingGroupName"
      ensure_worker_capacity "$(stack_output PythonLinuxArm64ClusterArn)" "PythonLinuxArm64AutoScalingGroupName"
      ensure_worker_capacity "$(stack_output PythonWindowsAmd64ClusterArn)" "PythonWindowsAmd64AutoScalingGroupName"
      ;;
  esac

  echo "Starting Python ECS scan: ${state_machine_arn}"
  execution_arn="$(
    aws stepfunctions start-execution \
      --state-machine-arn "${state_machine_arn}" \
      --name "${execution_name}" \
      --input "$(cat <<EOF
{"input_bucket":"${INPUT_BUCKET}","input_object_key":"${INPUT_OBJECT_KEY}","evidence_bucket":"${EVIDENCE_BUCKET}","evidence_prefix":"${EVIDENCE_PREFIX}","ephemeral_bucket":"${EPHEMERAL_BUCKET}","ephemeral_prefix":"${EPHEMERAL_PREFIX}","remediate_medium":"${REMEDIATE_MEDIUM}","fail_on_medium":"${FAIL_ON_MEDIUM}","safety_api_key":"${SAFETY_API_KEY}","scan_timestamp":"${timestamp}","scan_execution_id":"${execution_name}","platforms":${platforms_json}}
EOF
)" \
      --query "executionArn" \
      --output text \
      "${AWS_ARGS[@]}"
  )"
  printf '%-18s %s\n' "ExecutionName" "${execution_name}"
  printf '%-18s %s\n' "ExecutionArn" "${execution_arn}"
  printf '%-18s s3://%s/%s/orchestration/python/%s/orchestration-summary.json\n' "SummaryPath" "${EVIDENCE_BUCKET}" "${EVIDENCE_PREFIX}" "${execution_name}"
}

if [[ "${PLATFORM_SET}" == "linux-only" || "${PLATFORM_SET}" == "linux-amd64" || "${PLATFORM_SET}" == "linux-arm64" || "${PLATFORM_SET}" == "windows-only" || "${STACK_NAME}" == *"-python-ecs" ]]; then
  start_ecs_scan
else
  printf '%-16s %-48s %s\n' "Platform" "ProjectName" "BuildId"
  start_build "linux-amd64" "LinuxAmd64ProjectName"
  start_build "linux-arm64" "LinuxArm64ProjectName"
  start_build "windows-amd64" "WindowsAmd64ProjectName"
fi
