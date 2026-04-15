#!/usr/bin/env bash
set -euo pipefail

TARGET_PLATFORM="${1:?platform argument required}"
TS="${SCAN_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
BASE_DIR="${SCAN_WORK_DIR:-/var/lib/package-scanner}"
RUN_DIR="${BASE_DIR}/scan-out/${TS}"
PROJECT_DIR="${RUN_DIR}/project"
CACHE_DIR="${RUN_DIR}/cache"
LIBRARY_DIR="${RUN_DIR}/library"
CHECKPOINT_PREFIX="s3://${EPHEMERAL_BUCKET}/${EPHEMERAL_PREFIX}/checkpoints/r/${SCAN_EXECUTION_ID:-manual}/${TARGET_PLATFORM}"
CHECKPOINT_INTERVAL_SECONDS="${CHECKPOINT_INTERVAL_SECONDS:-900}"
SCRIPT_ROOT="${SCRIPT_ROOT:-/opt/package-scanner/scripts}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

checkpoint_pid=""
INPUT_FILE=""
INPUT_KIND=""
INPUT_R_VERSION=""

write_state() {
  local phase="$1"
  cat > "${RUN_DIR}/stage-state.json" <<EOF
{"platform":"${TARGET_PLATFORM}","scan_execution_id":"${SCAN_EXECUTION_ID:-manual}","scan_timestamp":"${TS}","phase":"${phase}","checkpoint_prefix":"${CHECKPOINT_PREFIX#s3://}"}
EOF
}

upload_if_exists() {
  local path="$1"
  local dest="$2"
  if [[ -f "${path}" ]]; then
    aws s3 cp "${path}" "${dest}" >/dev/null
  fi
  return 0
}

publish_checkpoint() {
  local phase="${1:-restore}"
  write_state "${phase}"
  if [[ -d "${CACHE_DIR}" ]]; then
    "${PYTHON_BIN}" "${SCRIPT_ROOT}/bundle-directory.py" \
      --source-dir "${CACHE_DIR}" \
      --output-file "${RUN_DIR}/checkpoint-renv-cache.tar.gz" \
      --checksum-file "${RUN_DIR}/checkpoint-renv-cache.tar.gz.sha256"
    aws s3 cp "${RUN_DIR}/checkpoint-renv-cache.tar.gz" "${CHECKPOINT_PREFIX}/latest/renv-cache.tar.gz" >/dev/null
    aws s3 cp "${RUN_DIR}/checkpoint-renv-cache.tar.gz.sha256" "${CHECKPOINT_PREFIX}/latest/renv-cache.tar.gz.sha256" >/dev/null
  fi
  if [[ -d "${LIBRARY_DIR}" ]]; then
    "${PYTHON_BIN}" "${SCRIPT_ROOT}/bundle-directory.py" \
      --source-dir "${LIBRARY_DIR}" \
      --output-file "${RUN_DIR}/checkpoint-renv-library.tar.gz" \
      --checksum-file "${RUN_DIR}/checkpoint-renv-library.tar.gz.sha256"
    aws s3 cp "${RUN_DIR}/checkpoint-renv-library.tar.gz" "${CHECKPOINT_PREFIX}/latest/renv-library.tar.gz" >/dev/null
    aws s3 cp "${RUN_DIR}/checkpoint-renv-library.tar.gz.sha256" "${CHECKPOINT_PREFIX}/latest/renv-library.tar.gz.sha256" >/dev/null
  fi
  aws s3 cp "${RUN_DIR}/stage-state.json" "${CHECKPOINT_PREFIX}/latest/stage-state.json" >/dev/null
}

stop_checkpoint_loop() {
  if [[ -n "${checkpoint_pid}" ]]; then
    kill "${checkpoint_pid}" >/dev/null 2>&1 || true
    wait "${checkpoint_pid}" 2>/dev/null || true
    checkpoint_pid=""
  fi
}

start_checkpoint_loop() {
  (
    while true; do
      sleep "${CHECKPOINT_INTERVAL_SECONDS}"
      publish_checkpoint "restore"
    done
  ) &
  checkpoint_pid="$!"
}

publish_failure_diagnostics() {
  set +e
  stop_checkpoint_loop
  write_state "failed"
  upload_if_exists "${RUN_DIR}/preflight-native-deps.txt" "${CHECKPOINT_PREFIX}/failures/preflight-native-deps.txt"
  upload_if_exists "${RUN_DIR}/preflight-native-deps.json" "${CHECKPOINT_PREFIX}/failures/preflight-native-deps.json"
  upload_if_exists "${RUN_DIR}/restore.log" "${CHECKPOINT_PREFIX}/failures/restore.log"
  upload_if_exists "${RUN_DIR}/restore-root-cause.txt" "${CHECKPOINT_PREFIX}/failures/restore-root-cause.txt"
  upload_if_exists "${RUN_DIR}/stage-state.json" "${CHECKPOINT_PREFIX}/failures/stage-state.json"
  publish_checkpoint "failed"
}
trap publish_failure_diagnostics ERR

mkdir -p "${RUN_DIR}" "${PROJECT_DIR}" "${CACHE_DIR}" "${LIBRARY_DIR}" /tmp/scan-input
INPUT_FILE="/tmp/scan-input/$(basename "${INPUT_OBJECT_KEY}")"
aws s3 cp "s3://${INPUT_BUCKET}/${INPUT_OBJECT_KEY}" "${INPUT_FILE}" >/dev/null
read -r INPUT_KIND INPUT_R_VERSION < <("${PYTHON_BIN}" - "${INPUT_FILE}" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8-sig") as handle:
    payload = json.load(handle)
if isinstance(payload, dict) and "Packages" in payload and "R" in payload:
    print("lockfile", payload["R"]["Version"])
elif isinstance(payload, dict) and (payload.get("input_type") == "requested-packages" or "packages" in payload):
    r = payload.get("r", {}) or {}
    version = r.get("version") or payload.get("r_version")
    if not version:
        raise SystemExit("requested-package manifest missing r.version")
    print("requested", version)
else:
    raise SystemExit("unsupported R input manifest")
PY
)
if [[ "${INPUT_KIND}" == "lockfile" ]]; then
  cp "${INPUT_FILE}" "${RUN_DIR}/renv.lock"
else
  cp "${INPUT_FILE}" "${RUN_DIR}/requested-packages.json"
fi

if aws s3 ls "${CHECKPOINT_PREFIX}/latest/renv-cache.tar.gz" >/dev/null 2>&1; then
  aws s3 cp "${CHECKPOINT_PREFIX}/latest/renv-cache.tar.gz" "${RUN_DIR}/checkpoint-renv-cache.tar.gz" >/dev/null
  "${PYTHON_BIN}" "${SCRIPT_ROOT}/extract-archive.py" --archive "${RUN_DIR}/checkpoint-renv-cache.tar.gz" --destination "${CACHE_DIR}"
fi
if aws s3 ls "${CHECKPOINT_PREFIX}/latest/renv-library.tar.gz" >/dev/null 2>&1; then
  aws s3 cp "${CHECKPOINT_PREFIX}/latest/renv-library.tar.gz" "${RUN_DIR}/checkpoint-renv-library.tar.gz" >/dev/null
  "${PYTHON_BIN}" "${SCRIPT_ROOT}/extract-archive.py" --archive "${RUN_DIR}/checkpoint-renv-library.tar.gz" --destination "${LIBRARY_DIR}"
fi

R_VERSION="${INPUT_R_VERSION}"
ACTUAL_R_VERSION="$(Rscript -e 'cat(as.character(getRversion()))')"
if [[ "${ACTUAL_R_VERSION}" != "${R_VERSION}" ]]; then
  echo "R version mismatch. image=${ACTUAL_R_VERSION} input=${R_VERSION}" >&2
  exit 1
fi

run_preflight() {
  write_state "preflight"
  "${PYTHON_BIN}" "${SCRIPT_ROOT}/preflight-r-native-deps.py" \
    --lock-file "${RUN_DIR}/renv.lock" \
    --platform "${TARGET_PLATFORM}" \
    --output-json "${RUN_DIR}/preflight-native-deps.json" \
    --output-text "${RUN_DIR}/preflight-native-deps.txt"
  upload_if_exists "${RUN_DIR}/preflight-native-deps.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/r/${TARGET_PLATFORM}/${TS}/preflight-native-deps.json"
  upload_if_exists "${RUN_DIR}/preflight-native-deps.txt" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/preflight-native-deps.txt"
}

if [[ "${INPUT_KIND}" == "lockfile" ]]; then
  run_preflight
fi

write_state "restore"
start_checkpoint_loop

materialize_args=(
  --project-dir "${PROJECT_DIR}"
  --cache-dir "${CACHE_DIR}"
  --library-dir "${LIBRARY_DIR}"
  --output-dir "${RUN_DIR}"
  --platform "${TARGET_PLATFORM}"
  --clean false
)
if [[ "${INPUT_KIND}" == "lockfile" ]]; then
  materialize_args=(--lock-file "${RUN_DIR}/renv.lock" "${materialize_args[@]}")
else
  materialize_args=(--requested-packages-file "${RUN_DIR}/requested-packages.json" "${materialize_args[@]}")
fi

Rscript "${SCRIPT_ROOT}/materialize-r-environment.R" "${materialize_args[@]}"

stop_checkpoint_loop
publish_checkpoint "restored"

if [[ "${INPUT_KIND}" == "requested" ]]; then
  run_preflight
fi

LIBRARY_PATH="$(cat "${RUN_DIR}/library-path.txt")"
environment_artifacts=(renv.lock installed-packages.csv session-info.txt renv-status.txt materialization-summary.json r-packages.cdx.json)
if [[ -f "${RUN_DIR}/requested-packages.json" ]]; then
  environment_artifacts+=(requested-packages.json)
fi
"${PYTHON_BIN}" "${SCRIPT_ROOT}/bundle-directory.py" --source-dir "${CACHE_DIR}" --output-file "${RUN_DIR}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz" --checksum-file "${RUN_DIR}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
"${PYTHON_BIN}" "${SCRIPT_ROOT}/bundle-directory.py" --source-dir "${LIBRARY_PATH}" --output-file "${RUN_DIR}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz" --checksum-file "${RUN_DIR}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
"${PYTHON_BIN}" "${SCRIPT_ROOT}/generate-r-materialization-summary.py" --run-dir "${RUN_DIR}" --platform "${TARGET_PLATFORM}" --r-version "${R_VERSION}" --cache-dir "${CACHE_DIR}" --library-path "${LIBRARY_PATH}"
"${PYTHON_BIN}" "${SCRIPT_ROOT}/generate-r-sbom.py" --installed-packages-file "${RUN_DIR}/installed-packages.csv" --out-file "${RUN_DIR}/r-packages.cdx.json"
"${PYTHON_BIN}" "${SCRIPT_ROOT}/scan-r-vulnerabilities.py" --installed-packages-file "${RUN_DIR}/installed-packages.csv" --lock-file "${RUN_DIR}/renv.lock" --out-file "${RUN_DIR}/osv-report.json"
trivy sbom --format json --output "${RUN_DIR}/trivy-sbom-report.json" "${RUN_DIR}/r-packages.cdx.json" || true
GOVERNANCE_EXIT=0
"${PYTHON_BIN}" "${SCRIPT_ROOT}/generate-r-governance-artifacts.py" --run-dir "${RUN_DIR}" --platform "${TARGET_PLATFORM}" --remediate-medium "${REMEDIATE_MEDIUM:-true}" --fail-on-medium "${FAIL_ON_MEDIUM:-false}" --remediate-unknown "${REMEDIATE_UNKNOWN:-true}" --fail-on-unknown "${FAIL_ON_UNKNOWN:-false}" || GOVERNANCE_EXIT=$?
tar -czf "${RUN_DIR}/environment-artifacts.tar.gz" -C "${RUN_DIR}" "${environment_artifacts[@]}" || true
aws s3 cp "${RUN_DIR}/" "s3://${EPHEMERAL_BUCKET}/${EPHEMERAL_PREFIX}/${TARGET_PLATFORM}/${TS}/" --recursive >/dev/null
aws s3 cp "${RUN_DIR}/renv.lock" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/r/${TARGET_PLATFORM}/${TS}/renv.lock" >/dev/null
upload_if_exists "${RUN_DIR}/requested-packages.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/r/${TARGET_PLATFORM}/${TS}/requested-packages.json"
upload_if_exists "${RUN_DIR}/approval-candidate-packages.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/r/${TARGET_PLATFORM}/${TS}/approval-candidate-packages.csv"
upload_if_exists "${RUN_DIR}/installed-packages.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/r/${TARGET_PLATFORM}/${TS}/installed-packages.csv"
upload_if_exists "${RUN_DIR}/r-packages.cdx.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/r-packages.cdx.json"
upload_if_exists "${RUN_DIR}/session-info.txt" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/session-info.txt"
upload_if_exists "${RUN_DIR}/renv-status.txt" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/renv-status.txt"
upload_if_exists "${RUN_DIR}/preflight-native-deps.txt" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/preflight-native-deps.txt"
upload_if_exists "${RUN_DIR}/restore.log" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/restore.log"
upload_if_exists "${RUN_DIR}/restore-root-cause.txt" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/r/${TARGET_PLATFORM}/${TS}/restore-root-cause.txt"
upload_if_exists "${RUN_DIR}/materialization-summary.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/r/${TARGET_PLATFORM}/${TS}/materialization-summary.json"
upload_if_exists "${RUN_DIR}/preflight-native-deps.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/r/${TARGET_PLATFORM}/${TS}/preflight-native-deps.json"
upload_if_exists "${RUN_DIR}/environment-artifacts.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/environment-artifacts.tar.gz"
upload_if_exists "${RUN_DIR}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz"
upload_if_exists "${RUN_DIR}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz.sha256" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
upload_if_exists "${RUN_DIR}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/packages/offline/r/${TARGET_PLATFORM}/${TS}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz"
upload_if_exists "${RUN_DIR}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz.sha256" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/packages/offline/r/${TARGET_PLATFORM}/${TS}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
upload_if_exists "${RUN_DIR}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/packages/offline/r/${TARGET_PLATFORM}/${TS}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz"
upload_if_exists "${RUN_DIR}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz.sha256" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/packages/offline/r/${TARGET_PLATFORM}/${TS}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
upload_if_exists "${RUN_DIR}/trivy-sbom-report.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/model-results/r/${TARGET_PLATFORM}/${TS}/trivy-sbom-report.json"
upload_if_exists "${RUN_DIR}/osv-report.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/model-results/r/${TARGET_PLATFORM}/${TS}/osv-report.json"
upload_if_exists "${RUN_DIR}/vulnerability-findings.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/r/${TARGET_PLATFORM}/${TS}/vulnerability-findings.csv"
upload_if_exists "${RUN_DIR}/remediation-required.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/r/${TARGET_PLATFORM}/${TS}/remediation-required.csv"
upload_if_exists "${RUN_DIR}/remediation-exceptions.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/r/${TARGET_PLATFORM}/${TS}/remediation-exceptions.csv"
upload_if_exists "${RUN_DIR}/remediation-spreadsheet.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/r/${TARGET_PLATFORM}/${TS}/remediation-spreadsheet.csv"
upload_if_exists "${RUN_DIR}/governance-summary.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/r/${TARGET_PLATFORM}/${TS}/governance-summary.json"
printf '{"platform":"%s","timestamp_utc":"%s","scan_execution_id":"%s","r_version":"%s","input_type":"%s","ephemeral_prefix":"%s/%s/%s","offline_bundle_prefix":"%s/packages/offline/r/%s/%s","cleanup":"requested"}' "${TARGET_PLATFORM}" "${TS}" "${SCAN_EXECUTION_ID:-manual}" "${R_VERSION}" "${INPUT_KIND}" "${EPHEMERAL_PREFIX}" "${TARGET_PLATFORM}" "${TS}" "${EVIDENCE_PREFIX}" "${TARGET_PLATFORM}" "${TS}" > "${RUN_DIR}/run-metadata.json"
aws s3 cp "${RUN_DIR}/run-metadata.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/r/${TARGET_PLATFORM}/${TS}/run-metadata.json" >/dev/null
write_state "completed"
aws s3 cp "${RUN_DIR}/stage-state.json" "${CHECKPOINT_PREFIX}/latest/stage-state.json" >/dev/null

if [[ "${GOVERNANCE_EXIT}" -ne 0 ]]; then
  echo "Governance gate failed with exit ${GOVERNANCE_EXIT}" >&2
  exit "${GOVERNANCE_EXIT}"
fi

exit 0
