#!/usr/bin/env bash
set -euo pipefail

EXECUTION_ARN=""
EXECUTION_NAME=""
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
PLATFORM_SET="linux-only"
LATEST="false"

usage() {
  cat <<'EOF'
Usage: poll-r-scan.sh [--execution-arn <arn> | --latest --stack-name <name>] [options]

Selection:
  --execution-arn <arn>                 Step Functions execution ARN to poll
  --latest                              Resolve the current/latest execution automatically
  --platform-set <all|linux-only>       Which R state machine to inspect for --latest (default: linux-only)

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
    --latest \
    --stack-name cyber-scanner-dev-r-ecs \
    --profile AdministratorAccess-123456789012 \
    --once
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execution-arn) EXECUTION_ARN="$2"; shift 2 ;;
    --latest) LATEST="true"; shift 1 ;;
    --platform-set) PLATFORM_SET="$2"; shift 2 ;;
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

if [[ "${PLATFORM_SET}" != "all" && "${PLATFORM_SET}" != "linux-only" ]]; then
  echo "--platform-set must be one of: all, linux-only" >&2
  exit 1
fi
if [[ "${LATEST}" != "true" && -z "${EXECUTION_ARN}" ]]; then
  echo "Provide either --execution-arn or --latest." >&2
  usage >&2
  exit 1
fi
if [[ "${LATEST}" == "true" && -z "${STACK_NAME}" ]]; then
  echo "--latest requires --stack-name." >&2
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

resolve_execution_arn() {
  local state_machine_key="RScanOrchestrationStateMachineArn"
  if [[ "${PLATFORM_SET}" == "linux-only" ]]; then
    state_machine_key="RLinuxScanOrchestrationStateMachineArn"
  fi
  local state_machine_arn
  state_machine_arn="$(stack_output "${state_machine_key}")"
  if [[ -z "${state_machine_arn}" || "${state_machine_arn}" == "None" ]]; then
    echo "Missing stack output: ${state_machine_key}" >&2
    exit 1
  fi

  local execution_arn
  execution_arn="$(
    aws stepfunctions list-executions \
      --state-machine-arn "${state_machine_arn}" \
      --status-filter RUNNING \
      --max-results 1 \
      --query 'executions[0].executionArn' \
      --output text \
      "${AWS_ARGS[@]}"
  )"

  if [[ -z "${execution_arn}" || "${execution_arn}" == "None" ]]; then
    execution_arn="$(
      aws stepfunctions list-executions \
        --state-machine-arn "${state_machine_arn}" \
        --max-results 1 \
        --query 'executions[0].executionArn' \
        --output text \
        "${AWS_ARGS[@]}"
    )"
  fi

  if [[ -z "${execution_arn}" || "${execution_arn}" == "None" ]]; then
    echo "No executions found for ${state_machine_arn}" >&2
    exit 1
  fi

  printf '%s\n' "${execution_arn}"
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
    local summary_json
    if summary_json="$(aws s3 cp "${summary_uri}" - "${AWS_ARGS[@]}")"; then
      printf '%s\n' "${summary_json}"
      SUMMARY_JSON="${summary_json}" python - <<'PY'
import json
import os

data = json.loads(os.environ["SUMMARY_JSON"])
for platform in data.get("platforms", []):
    print(f"Platform: {platform.get('platform')}")
    print(f"  Status: {platform.get('status')}")
    if platform.get("error"):
        print(f"  Error: {platform.get('error')}")
    if platform.get("cause"):
        print(f"  Cause: {platform.get('cause')}")
    missing = platform.get("missing") or []
    if missing:
        print("  Missing:")
        for item in missing:
            print(f"    - {item}")
PY
    fi
  else
    echo "SummaryObject: missing"
  fi
}

if [[ "${LATEST}" == "true" ]]; then
  EXECUTION_ARN="$(resolve_execution_arn)"
fi
EXECUTION_NAME="${EXECUTION_NAME:-$(execution_name_from_arn "${EXECUTION_ARN}")}"

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
