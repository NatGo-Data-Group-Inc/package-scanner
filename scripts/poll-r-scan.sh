#!/usr/bin/env bash
set -euo pipefail

EXECUTION_ARN=""
STACK_NAME=""
EVIDENCE_BUCKET=""
EVIDENCE_PREFIX="evidence"
REGION="us-east-1"
PROFILE=""
ALLOW_DEFAULT_PROFILE="false"
POLL_SECONDS=900
ONCE="false"
LINUX_CLUSTER_NAME="package-scanner-dev-r-linux"
WINDOWS_CLUSTER_NAME="package-scanner-dev-r-windows"
CHECK_LINUX_CLUSTER="true"
CHECK_WINDOWS_CLUSTER="false"

usage() {
  cat <<'EOF'
Usage: poll-r-scan.sh --execution-arn <arn> [options]

Required:
  --execution-arn <arn>                 Step Functions execution ARN to poll

Optional:
  --stack-name <name>                   Resolve EvidenceBucketName from stack output
  --evidence-bucket <bucket>            Override evidence bucket directly
  --evidence-prefix <prefix>            (default: evidence)
  --region <region>                     (default: us-east-1)
  --profile <profile>
  --allow-default-profile
  --poll-seconds <seconds>              (default: 900)
  --once                                Check once and exit
  --linux-cluster-name <name>           (default: package-scanner-dev-r-linux)
  --windows-cluster-name <name>         (default: package-scanner-dev-r-windows)
  --skip-linux-cluster                  Do not query Linux ECS task status
  --check-windows-cluster               Also query Windows ECS task status

Examples:
  ./scripts/poll-r-scan.sh \
    --execution-arn arn:aws:states:us-east-1:123456789012:execution:sm:r-scan-20260324T000135Z-0ec08ba8 \
    --stack-name cyber-scanner-dev-r-ecs \
    --profile AdministratorAccess-123456789012

  ./scripts/poll-r-scan.sh \
    --execution-arn arn:aws:states:us-east-1:123456789012:execution:sm:r-scan-20260324T000135Z-0ec08ba8 \
    --evidence-bucket my-evidence-bucket \
    --once
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execution-arn) EXECUTION_ARN="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --evidence-bucket) EVIDENCE_BUCKET="$2"; shift 2 ;;
    --evidence-prefix) EVIDENCE_PREFIX="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --allow-default-profile) ALLOW_DEFAULT_PROFILE="true"; shift 1 ;;
    --poll-seconds) POLL_SECONDS="$2"; shift 2 ;;
    --once) ONCE="true"; shift 1 ;;
    --linux-cluster-name) LINUX_CLUSTER_NAME="$2"; shift 2 ;;
    --windows-cluster-name) WINDOWS_CLUSTER_NAME="$2"; shift 2 ;;
    --skip-linux-cluster) CHECK_LINUX_CLUSTER="false"; shift 1 ;;
    --check-windows-cluster) CHECK_WINDOWS_CLUSTER="true"; shift 1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${EXECUTION_ARN}" ]]; then
  echo "--execution-arn is required." >&2
  usage >&2
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

stack_output() {
  local key="$1"
  aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue | [0]" \
    --output text \
    "${AWS_ARGS[@]}"
}

if [[ -z "${EVIDENCE_BUCKET}" && -n "${STACK_NAME}" ]]; then
  EVIDENCE_BUCKET="$(stack_output EvidenceBucketName)"
fi

execution_name_from_arn() {
  python - "$1" <<'PY'
import sys
print(sys.argv[1].rsplit(":", 1)[-1])
PY
}

json_field() {
  local json_text="$1"
  local field="$2"
  printf '%s' "${json_text}" | python -c '
import json, sys
field = sys.argv[1]
data = json.load(sys.stdin)
value = data.get(field, "")
if isinstance(value, (dict, list)):
    print(json.dumps(value))
elif value is None:
    print("")
else:
    print(value)
' "${field}"
}

list_cluster_tasks() {
  local cluster_name="$1"
  local label="$2"
  local tasks
  if ! tasks="$(aws ecs list-tasks --cluster "${cluster_name}" --query 'taskArns' --output text "${AWS_ARGS[@]}" 2>/dev/null)"; then
    echo "${label}: unavailable"
    return 0
  fi
  if [[ -z "${tasks}" || "${tasks}" == "None" ]]; then
    echo "${label}: no active tasks"
  else
    echo "${label}: ${tasks}"
  fi
}

print_summary_if_present() {
  if [[ -z "${EVIDENCE_BUCKET}" ]]; then
    return 0
  fi
  local execution_name="$1"
  local summary_key="${EVIDENCE_PREFIX}/orchestration/r/${execution_name}/orchestration-summary.json"
  local summary_uri="s3://${EVIDENCE_BUCKET}/${summary_key}"
  echo "SummaryPath: ${summary_uri}"
  if aws s3api head-object --bucket "${EVIDENCE_BUCKET}" --key "${summary_key}" "${AWS_ARGS[@]}" >/dev/null 2>&1; then
    echo "SummaryObject: present"
    aws s3 cp "${summary_uri}" - "${AWS_ARGS[@]}" || true
  else
    echo "SummaryObject: missing"
  fi
}

EXECUTION_NAME="$(execution_name_from_arn "${EXECUTION_ARN}")"

while true; do
  NOW="$(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "[$NOW] Polling ${EXECUTION_NAME}"

  EXECUTION_JSON="$(
    aws stepfunctions describe-execution \
      --execution-arn "${EXECUTION_ARN}" \
      "${AWS_ARGS[@]}"
  )"

  STATUS="$(json_field "${EXECUTION_JSON}" status)"
  START_DATE="$(json_field "${EXECUTION_JSON}" startDate)"
  STOP_DATE="$(json_field "${EXECUTION_JSON}" stopDate)"
  ERROR_FIELD="$(json_field "${EXECUTION_JSON}" error)"
  CAUSE_FIELD="$(json_field "${EXECUTION_JSON}" cause)"

  echo "ExecutionStatus: ${STATUS}"
  [[ -n "${START_DATE}" ]] && echo "StartDate: ${START_DATE}"
  [[ -n "${STOP_DATE}" ]] && echo "StopDate: ${STOP_DATE}"

  if [[ "${CHECK_LINUX_CLUSTER}" == "true" ]]; then
    list_cluster_tasks "${LINUX_CLUSTER_NAME}" "LinuxCluster"
  fi
  if [[ "${CHECK_WINDOWS_CLUSTER}" == "true" ]]; then
    list_cluster_tasks "${WINDOWS_CLUSTER_NAME}" "WindowsCluster"
  fi

  if [[ "${STATUS}" == "SUCCEEDED" || "${STATUS}" == "FAILED" || "${STATUS}" == "TIMED_OUT" || "${STATUS}" == "ABORTED" ]]; then
    [[ -n "${ERROR_FIELD}" ]] && echo "Error: ${ERROR_FIELD}"
    [[ -n "${CAUSE_FIELD}" ]] && echo "Cause: ${CAUSE_FIELD}"
    print_summary_if_present "${EXECUTION_NAME}"
    if [[ "${STATUS}" == "SUCCEEDED" ]]; then
      exit 0
    fi
    exit 1
  fi

  if [[ "${ONCE}" == "true" ]]; then
    exit 0
  fi

  echo "Sleeping ${POLL_SECONDS}s"
  sleep "${POLL_SECONDS}"
done
