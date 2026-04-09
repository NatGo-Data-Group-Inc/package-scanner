#!/usr/bin/env bash
set -euo pipefail

STACK_NAME=""
INPUT_BUCKET=""
SHARD_DIR=""
CANDIDATE_ID=""
INPUT_PREFIX="inputs/r/candidates"
PROFILE=""
REGION="us-east-1"
EVIDENCE_BUCKET=""
EPHEMERAL_BUCKET=""
DEPLOYMENT_LOCK_TOKEN=""
PLATFORM_SET="linux-only"
MAX_SHARDS=""
LAUNCH_DELAY_SECONDS="2"

usage() {
  cat <<'EOF'
Usage: start-r-triage-shards.sh --stack-name <name> --input-bucket <bucket> --shard-dir <dir> [options]

Required:
  --stack-name <name>
  --input-bucket <bucket>
  --shard-dir <dir>                     Directory containing triage-plan.json and shard manifests
  --deployment-lock-token <token>

Optional:
  --candidate-id <id>                   Override candidate id from triage-plan.json
  --input-prefix <prefix>               (default: inputs/r/candidates)
  --profile <profile>
  --region <region>                     (default: us-east-1)
  --evidence-bucket <bucket>
  --ephemeral-bucket <bucket>
  --platform-set <all|linux-only>       (default: linux-only)
  --max-shards <count>                  Launch only the first N shards in plan order
  --launch-delay-seconds <seconds>      (default: 2)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --input-bucket) INPUT_BUCKET="$2"; shift 2 ;;
    --shard-dir) SHARD_DIR="$2"; shift 2 ;;
    --candidate-id) CANDIDATE_ID="$2"; shift 2 ;;
    --input-prefix) INPUT_PREFIX="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --evidence-bucket) EVIDENCE_BUCKET="$2"; shift 2 ;;
    --ephemeral-bucket) EPHEMERAL_BUCKET="$2"; shift 2 ;;
    --deployment-lock-token) DEPLOYMENT_LOCK_TOKEN="$2"; shift 2 ;;
    --platform-set) PLATFORM_SET="$2"; shift 2 ;;
    --max-shards) MAX_SHARDS="$2"; shift 2 ;;
    --launch-delay-seconds) LAUNCH_DELAY_SECONDS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${STACK_NAME}" || -z "${INPUT_BUCKET}" || -z "${SHARD_DIR}" || -z "${DEPLOYMENT_LOCK_TOKEN}" ]]; then
  echo "Missing required arguments." >&2
  usage >&2
  exit 1
fi

PLAN_PATH="${SHARD_DIR%/}/triage-plan.json"
if [[ ! -f "${PLAN_PATH}" ]]; then
  echo "triage-plan.json not found in ${SHARD_DIR}" >&2
  exit 1
fi

readarray -t SHARD_FILES < <(
  python - "${PLAN_PATH}" "${MAX_SHARDS}" <<'PY'
import json
import sys
plan_path, max_shards = sys.argv[1], sys.argv[2]
with open(plan_path, encoding="utf-8") as handle:
    plan = json.load(handle)
shards = [item["filename"] for item in plan.get("shards", [])]
if max_shards:
    shards = shards[: max(0, int(max_shards))]
for shard in shards:
    print(shard)
PY
)

if [[ ${#SHARD_FILES[@]} -eq 0 ]]; then
  echo "No shard manifests found to launch." >&2
  exit 1
fi

if [[ -z "${CANDIDATE_ID}" ]]; then
  CANDIDATE_ID="$(
    python - "${PLAN_PATH}" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    plan = json.load(handle)
print(plan.get("candidate_id", "candidate"))
PY
  )"
fi

TRIAGE_TS="$(date -u +%Y%m%dT%H%M%SZ)"

for shard_file in "${SHARD_FILES[@]}"; do
  shard_path="${SHARD_DIR%/}/${shard_file}"
  if [[ ! -f "${shard_path}" ]]; then
    echo "Shard manifest missing: ${shard_path}" >&2
    exit 1
  fi
  shard_id="${shard_file%.requested-packages.json}"
  input_key="${INPUT_PREFIX%/}/${CANDIDATE_ID}/triage/${TRIAGE_TS}/${shard_id}/requested-packages.json"
  cmd=(
    ./scripts/start-r-scan.sh
    --stack-name "${STACK_NAME}"
    --input-bucket "${INPUT_BUCKET}"
    --input-object-key "${input_key}"
    --source-requested-file "${shard_path}"
    --deployment-lock-token "${DEPLOYMENT_LOCK_TOKEN}"
    --platform-set "${PLATFORM_SET}"
    --execution-prefix "r-triage"
  )
  if [[ -n "${PROFILE}" ]]; then
    cmd+=(--profile "${PROFILE}")
  fi
  if [[ -n "${REGION}" ]]; then
    cmd+=(--region "${REGION}")
  fi
  if [[ -n "${EVIDENCE_BUCKET}" ]]; then
    cmd+=(--evidence-bucket "${EVIDENCE_BUCKET}")
  fi
  if [[ -n "${EPHEMERAL_BUCKET}" ]]; then
    cmd+=(--ephemeral-bucket "${EPHEMERAL_BUCKET}")
  fi
  "${cmd[@]}"
  sleep "${LAUNCH_DELAY_SECONDS}"
done
