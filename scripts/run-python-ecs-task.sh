#!/usr/bin/env bash
set -euo pipefail

TARGET_PLATFORM="${1:?platform argument required}"
TS="${SCAN_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
BASE_DIR="${SCAN_WORK_DIR:-/var/lib/package-scanner}"
RUN_DIR="${BASE_DIR}/scan-out/${TS}"
ROOT_PREFIX="${BASE_DIR}/micromamba"
ENV_PREFIX="${ROOT_PREFIX}/envs/target"
CHECKPOINT_PREFIX="s3://${EPHEMERAL_BUCKET}/${EPHEMERAL_PREFIX}/checkpoints/python/${SCAN_EXECUTION_ID:-manual}/${TARGET_PLATFORM}"
CHECKPOINT_INTERVAL_SECONDS="${CHECKPOINT_INTERVAL_SECONDS:-900}"
SCRIPT_ROOT="${SCRIPT_ROOT:-/opt/package-scanner/scripts}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
MAMBA_BIN="${MAMBA_BIN:-/usr/local/bin/micromamba}"

checkpoint_pid=""

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
  local phase="${1:-materialize}"
  write_state "${phase}"
  if [[ -d "${ROOT_PREFIX}/pkgs" ]]; then
    "${PYTHON_BIN}" "${SCRIPT_ROOT}/bundle-directory.py" \
      --source-dir "${ROOT_PREFIX}/pkgs" \
      --output-file "${RUN_DIR}/checkpoint-python-pkgs.tar.gz" \
      --checksum-file "${RUN_DIR}/checkpoint-python-pkgs.tar.gz.sha256"
    aws s3 cp "${RUN_DIR}/checkpoint-python-pkgs.tar.gz" "${CHECKPOINT_PREFIX}/latest/python-pkgs.tar.gz" >/dev/null
    aws s3 cp "${RUN_DIR}/checkpoint-python-pkgs.tar.gz.sha256" "${CHECKPOINT_PREFIX}/latest/python-pkgs.tar.gz.sha256" >/dev/null
  fi
  if [[ -d "${ENV_PREFIX}" ]]; then
    "${PYTHON_BIN}" "${SCRIPT_ROOT}/bundle-directory.py" \
      --source-dir "${ENV_PREFIX}" \
      --output-file "${RUN_DIR}/checkpoint-python-env.tar.gz" \
      --checksum-file "${RUN_DIR}/checkpoint-python-env.tar.gz.sha256"
    aws s3 cp "${RUN_DIR}/checkpoint-python-env.tar.gz" "${CHECKPOINT_PREFIX}/latest/python-env.tar.gz" >/dev/null
    aws s3 cp "${RUN_DIR}/checkpoint-python-env.tar.gz.sha256" "${CHECKPOINT_PREFIX}/latest/python-env.tar.gz.sha256" >/dev/null
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
      publish_checkpoint "materialize"
    done
  ) &
  checkpoint_pid="$!"
}

publish_failure_diagnostics() {
  set +e
  stop_checkpoint_loop
  write_state "failed"
  upload_if_exists "${RUN_DIR}/restore.log" "${CHECKPOINT_PREFIX}/failures/restore.log"
  upload_if_exists "${RUN_DIR}/stage-state.json" "${CHECKPOINT_PREFIX}/failures/stage-state.json"
  publish_checkpoint "failed"
}
trap publish_failure_diagnostics ERR

mkdir -p "${RUN_DIR}" "${ROOT_PREFIX}" /tmp/scan-input
aws s3 cp "s3://${INPUT_BUCKET}/${INPUT_OBJECT_KEY}" /tmp/scan-input/environment.yml >/dev/null
cp /tmp/scan-input/environment.yml "${RUN_DIR}/environment.yml"

if aws s3 ls "${CHECKPOINT_PREFIX}/latest/python-pkgs.tar.gz" >/dev/null 2>&1; then
  aws s3 cp "${CHECKPOINT_PREFIX}/latest/python-pkgs.tar.gz" "${RUN_DIR}/checkpoint-python-pkgs.tar.gz" >/dev/null
  mkdir -p "${ROOT_PREFIX}/pkgs"
  "${PYTHON_BIN}" "${SCRIPT_ROOT}/extract-archive.py" --archive "${RUN_DIR}/checkpoint-python-pkgs.tar.gz" --destination "${ROOT_PREFIX}/pkgs"
fi
if aws s3 ls "${CHECKPOINT_PREFIX}/latest/python-env.tar.gz" >/dev/null 2>&1; then
  aws s3 cp "${CHECKPOINT_PREFIX}/latest/python-env.tar.gz" "${RUN_DIR}/checkpoint-python-env.tar.gz" >/dev/null
  mkdir -p "${ENV_PREFIX}"
  "${PYTHON_BIN}" "${SCRIPT_ROOT}/extract-archive.py" --archive "${RUN_DIR}/checkpoint-python-env.tar.gz" --destination "${ENV_PREFIX}"
fi

write_state "materialize"
start_checkpoint_loop

{
  "${MAMBA_BIN}" create -r "${ROOT_PREFIX}" -y -n target -f "${RUN_DIR}/environment.yml" ||
  "${MAMBA_BIN}" env update -r "${ROOT_PREFIX}" -n target -f "${RUN_DIR}/environment.yml"
} > "${RUN_DIR}/restore.log" 2>&1

stop_checkpoint_loop
publish_checkpoint "restored"

PYTHON_VERSION="$("${MAMBA_BIN}" run -r "${ROOT_PREFIX}" -n target python - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
)"

"${MAMBA_BIN}" run -r "${ROOT_PREFIX}" -n target python -m pip list --format=freeze > "${RUN_DIR}/requirements.lock.txt"
"${MAMBA_BIN}" list -r "${ROOT_PREFIX}" -n target --json > "${RUN_DIR}/conda-list.json"
printf '%s\n' "${ENV_PREFIX}" > "${RUN_DIR}/env-prefix.txt"
"${PYTHON_BIN}" "${SCRIPT_ROOT}/generate-python-materialization-summary.py" \
  --run-dir "${RUN_DIR}" \
  --platform "${TARGET_PLATFORM}" \
  --python-version "${PYTHON_VERSION}" \
  --root-prefix "${ROOT_PREFIX}" \
  --env-prefix "${ENV_PREFIX}"

python -m cyclonedx_py requirements "${RUN_DIR}/requirements.lock.txt" -o "${RUN_DIR}/python-packages.cdx.json" || true
trivy sbom --format json --output "${RUN_DIR}/trivy-sbom-report.json" "${RUN_DIR}/python-packages.cdx.json" || true
safety scan --file "${RUN_DIR}/requirements.lock.txt" --output json > "${RUN_DIR}/safety-report.json" || true
GOVERNANCE_EXIT=0
"${PYTHON_BIN}" "${SCRIPT_ROOT}/generate-governance-artifacts.py" \
  --run-dir "${RUN_DIR}" \
  --platform "${TARGET_PLATFORM}" \
  --remediate-medium "${REMEDIATE_MEDIUM:-true}" \
  --fail-on-medium "${FAIL_ON_MEDIUM:-false}" || GOVERNANCE_EXIT=$?

"${PYTHON_BIN}" "${SCRIPT_ROOT}/bundle-directory.py" \
  --source-dir "${ROOT_PREFIX}/pkgs" \
  --output-file "${RUN_DIR}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz" \
  --checksum-file "${RUN_DIR}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
"${PYTHON_BIN}" "${SCRIPT_ROOT}/bundle-directory.py" \
  --source-dir "${ENV_PREFIX}" \
  --output-file "${RUN_DIR}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz" \
  --checksum-file "${RUN_DIR}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
tar -czf "${RUN_DIR}/environment-artifacts.tar.gz" -C "${RUN_DIR}" environment.yml requirements.lock.txt conda-list.json python-packages.cdx.json materialization-summary.json || true

aws s3 cp "${RUN_DIR}/" "s3://${EPHEMERAL_BUCKET}/${EPHEMERAL_PREFIX}/${TARGET_PLATFORM}/${TS}/" --recursive >/dev/null
aws s3 cp "${RUN_DIR}/environment.yml" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${TS}/environment.yml" >/dev/null
upload_if_exists "${RUN_DIR}/requirements.lock.txt" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${TS}/requirements.lock.txt"
upload_if_exists "${RUN_DIR}/approval-candidate-packages.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${TS}/approval-candidate-packages.csv"
upload_if_exists "${RUN_DIR}/python-packages.cdx.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${TS}/python-packages.cdx.json"
upload_if_exists "${RUN_DIR}/conda-list.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${TS}/conda-list.json"
upload_if_exists "${RUN_DIR}/environment-artifacts.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${TS}/environment-artifacts.tar.gz"
upload_if_exists "${RUN_DIR}/restore.log" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${TS}/restore.log"
upload_if_exists "${RUN_DIR}/materialization-summary.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/python/${TARGET_PLATFORM}/${TS}/materialization-summary.json"
upload_if_exists "${RUN_DIR}/trivy-sbom-report.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/model-results/python/${TARGET_PLATFORM}/${TS}/trivy-sbom-report.json"
upload_if_exists "${RUN_DIR}/safety-report.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/model-results/python/${TARGET_PLATFORM}/${TS}/safety-report.json"
upload_if_exists "${RUN_DIR}/vulnerability-findings.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/python/${TARGET_PLATFORM}/${TS}/vulnerability-findings.csv"
upload_if_exists "${RUN_DIR}/remediation-required.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/python/${TARGET_PLATFORM}/${TS}/remediation-required.csv"
upload_if_exists "${RUN_DIR}/remediation-exceptions.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/python/${TARGET_PLATFORM}/${TS}/remediation-exceptions.csv"
upload_if_exists "${RUN_DIR}/remediation-spreadsheet.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/python/${TARGET_PLATFORM}/${TS}/remediation-spreadsheet.csv"
upload_if_exists "${RUN_DIR}/governance-summary.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/python/${TARGET_PLATFORM}/${TS}/governance-summary.json"
upload_if_exists "${RUN_DIR}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/packages/offline/python/${TARGET_PLATFORM}/${TS}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz"
upload_if_exists "${RUN_DIR}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz.sha256" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/packages/offline/python/${TARGET_PLATFORM}/${TS}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
upload_if_exists "${RUN_DIR}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${TS}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz"
upload_if_exists "${RUN_DIR}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz.sha256" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${TS}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
printf '{"platform":"%s","timestamp_utc":"%s","scan_execution_id":"%s","python_version":"%s","ephemeral_prefix":"%s/%s/%s","offline_bundle_prefix":"%s/packages/offline/python/%s/%s","cleanup":"requested"}' "${TARGET_PLATFORM}" "${TS}" "${SCAN_EXECUTION_ID:-manual}" "${PYTHON_VERSION}" "${EPHEMERAL_PREFIX}" "${TARGET_PLATFORM}" "${TS}" "${EVIDENCE_PREFIX}" "${TARGET_PLATFORM}" "${TS}" > "${RUN_DIR}/run-metadata.json"
aws s3 cp "${RUN_DIR}/run-metadata.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/python/${TARGET_PLATFORM}/${TS}/run-metadata.json" >/dev/null
write_state "completed"
aws s3 cp "${RUN_DIR}/stage-state.json" "${CHECKPOINT_PREFIX}/latest/stage-state.json" >/dev/null

if [[ "${GOVERNANCE_EXIT}" -ne 0 ]]; then
  echo "Governance gate failed with exit ${GOVERNANCE_EXIT}" >&2
  exit "${GOVERNANCE_EXIT}"
fi

exit 0
