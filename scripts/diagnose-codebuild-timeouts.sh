#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME=""
REGION="us-east-1"
PROFILE=""
QUOTA_CODE=""
OUTPUT_FILE=""
STOP_STARTED_BUILDS="true"
INCLUDE_NO_OVERRIDE="false"
OVERRIDE_TIMEOUTS=()

usage() {
  cat <<'EOF'
Usage: diagnose-codebuild-timeouts.sh --project-name <name> [options]

Required:
  --project-name <name>

Optional:
  --region <region>                  (default: us-east-1)
  --profile <profile>
  --quota-code <code>                Optional Service Quotas quota code to query
  --include-no-override              Probe one build without timeout override
  --override-timeout <minutes>       Can be passed multiple times
  --stop-started-builds <true|false> (default: true)
  --output-file <path>               Write JSON report to path
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-name) PROJECT_NAME="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --quota-code) QUOTA_CODE="$2"; shift 2 ;;
    --include-no-override) INCLUDE_NO_OVERRIDE="true"; shift 1 ;;
    --override-timeout) OVERRIDE_TIMEOUTS+=("$2"); shift 2 ;;
    --stop-started-builds) STOP_STARTED_BUILDS="$2"; shift 2 ;;
    --output-file) OUTPUT_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${PROJECT_NAME}" ]]; then
  echo "--project-name is required." >&2
  usage >&2
  exit 1
fi

AWS_ARGS=(--region "${REGION}")
if [[ -n "${PROFILE}" ]]; then
  AWS_ARGS+=(--profile "${PROFILE}")
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

PROJECT_JSON="$(aws codebuild batch-get-projects --names "${PROJECT_NAME}" "${AWS_ARGS[@]}")"
printf '%s\n' "${PROJECT_JSON}" > "${TMP_DIR}/project.json"

QUOTA_JSON="null"
if [[ -n "${QUOTA_CODE}" ]]; then
  if QUOTA_JSON_RAW="$(aws service-quotas get-service-quota --service-code codebuild --quota-code "${QUOTA_CODE}" "${AWS_ARGS[@]}" 2>"${TMP_DIR}/quota.stderr")"; then
    QUOTA_JSON="${QUOTA_JSON_RAW}"
  else
    QUOTA_JSON="$(python - <<PY
import json
from pathlib import Path
print(json.dumps({"error": Path(${TMP_DIR@Q} + "/quota.stderr").read_text(encoding="utf-8")}))
PY
)"
  fi
fi
printf '%s\n' "${QUOTA_JSON}" > "${TMP_DIR}/quota.json"

PROBE_FILE="${TMP_DIR}/probes.jsonl"
: > "${PROBE_FILE}"

probes=()
if [[ "${INCLUDE_NO_OVERRIDE}" == "true" ]]; then
  probes+=("none")
fi
if [[ ${#OVERRIDE_TIMEOUTS[@]} -eq 0 && "${INCLUDE_NO_OVERRIDE}" != "true" ]]; then
  probes=("none" "60" "120" "480")
else
  seen=" "
  for t in "${OVERRIDE_TIMEOUTS[@]}"; do
    if [[ "${seen}" != *" ${t} "* ]]; then
      probes+=("${t}")
      seen+="$(printf '%s ' "${t}")"
    fi
  done
fi

for probe in "${probes[@]}"; do
  if [[ "${probe}" == "none" ]]; then
    BUILD_JSON="$(aws codebuild start-build --project-name "${PROJECT_NAME}" "${AWS_ARGS[@]}")"
    REQUESTED_OVERRIDE="null"
  else
    BUILD_JSON="$(aws codebuild start-build --project-name "${PROJECT_NAME}" --timeout-in-minutes-override "${probe}" "${AWS_ARGS[@]}")"
    REQUESTED_OVERRIDE="${probe}"
  fi

  printf '%s\n' "${BUILD_JSON}" > "${TMP_DIR}/build.json"
  BUILD_ID="$(
    python - <<PY
import json
print(json.load(open(${TMP_DIR@Q} + "/build.json"))["build"]["id"])
PY
  )"

  STOP_STATUS="null"
  STOP_PHASE="null"
  STOP_ERROR="null"
  if [[ "${STOP_STARTED_BUILDS}" == "true" ]]; then
    sleep 1
    if STOP_JSON="$(aws codebuild stop-build --id "${BUILD_ID}" "${AWS_ARGS[@]}" 2>"${TMP_DIR}/stop.stderr")"; then
      printf '%s\n' "${STOP_JSON}" > "${TMP_DIR}/stop.json"
      STOP_STATUS="$(
        python - <<PY
import json
print(json.dumps(json.load(open(${TMP_DIR@Q} + "/stop.json"))["build"].get("buildStatus")))
PY
      )"
      STOP_PHASE="$(
        python - <<PY
import json
print(json.dumps(json.load(open(${TMP_DIR@Q} + "/stop.json"))["build"].get("currentPhase")))
PY
      )"
    else
      STOP_ERROR="$(
        python - <<PY
import json
from pathlib import Path
print(json.dumps(Path(${TMP_DIR@Q} + "/stop.stderr").read_text(encoding="utf-8")))
PY
      )"
    fi
  fi

  python - <<PY >> "${PROBE_FILE}"
import json

build = json.load(open(${TMP_DIR@Q} + "/build.json"))["build"]
record = {
    "requested_timeout_override": json.loads(${REQUESTED_OVERRIDE@Q}),
    "id": build.get("id"),
    "project_name": build.get("projectName"),
    "initiator": build.get("initiator"),
    "build_status": build.get("buildStatus"),
    "current_phase": build.get("currentPhase"),
    "timeout_in_minutes": build.get("timeoutInMinutes"),
    "queued_timeout_in_minutes": build.get("queuedTimeoutInMinutes"),
    "start_time": build.get("startTime"),
    "stop_requested": ${STOP_STARTED_BUILDS@Q} == "true",
    "stopped_build_status": json.loads(${STOP_STATUS@Q}),
    "stopped_current_phase": json.loads(${STOP_PHASE@Q}),
    "stop_error": json.loads(${STOP_ERROR@Q}),
}
print(json.dumps(record))
PY
done

REPORT_JSON="$(
  python - <<PY
import json
from datetime import datetime, timezone
from pathlib import Path

project_payload = json.load(open(${TMP_DIR@Q} + "/project.json"))
projects = project_payload.get("projects", [])
if not projects:
    raise SystemExit("Project not found in batch-get-projects response")
project = projects[0]
quota_path = Path(${TMP_DIR@Q} + "/quota.json")
quota_payload = json.load(open(quota_path)) if quota_path.exists() else None
probe_results = [json.loads(line) for line in Path(${TMP_DIR@Q} + "/probes.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

report = {
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "project_name": ${PROJECT_NAME@Q},
    "region": ${REGION@Q},
    "profile": ${PROFILE@Q} or None,
    "project": {
        "name": project.get("name"),
        "arn": project.get("arn"),
        "environment_type": (project.get("environment") or {}).get("type"),
        "image": (project.get("environment") or {}).get("image"),
        "compute_type": (project.get("environment") or {}).get("computeType"),
        "timeout_in_minutes": project.get("timeoutInMinutes"),
        "queued_timeout_in_minutes": project.get("queuedTimeoutInMinutes"),
        "last_modified": project.get("lastModified"),
    },
    "service_quota": None if quota_payload in (None, "null") else quota_payload.get("Quota", quota_payload),
    "probe_results": probe_results,
}
print(json.dumps(report, indent=2, sort_keys=True))
PY
)"

printf '%s\n' "${REPORT_JSON}"
if [[ -n "${OUTPUT_FILE}" ]]; then
  printf '%s\n' "${REPORT_JSON}" > "${OUTPUT_FILE}"
fi
