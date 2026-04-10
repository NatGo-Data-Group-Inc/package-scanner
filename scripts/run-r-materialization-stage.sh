#!/usr/bin/env bash
set -euo pipefail

TARGET_PLATFORM="${1:?platform argument required}"
TS="${SCAN_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="/tmp/scan-out/${TS}"
PROJECT_DIR="${RUN_DIR}/project"
CACHE_DIR="${RUN_DIR}/cache"
LIBRARY_DIR="${RUN_DIR}/library"
CHECKPOINT_PREFIX="s3://${EPHEMERAL_BUCKET}/${EPHEMERAL_PREFIX}/checkpoints/r/${SCAN_EXECUTION_ID:-manual}/${TARGET_PLATFORM}"
STAGE_INDEX="${STAGE_INDEX:-1}"
TOTAL_STAGES="${TOTAL_STAGES:-1}"
FINAL_STAGE="${FINAL_STAGE:-true}"

mkdir -p "${RUN_DIR}" "${PROJECT_DIR}" "${CACHE_DIR}" "${LIBRARY_DIR}"
aws s3 cp "s3://${INPUT_BUCKET}/${INPUT_OBJECT_KEY}" /tmp/scan-input/renv.lock
cp /tmp/scan-input/renv.lock "${RUN_DIR}/renv.lock"
R_VERSION="$(python -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8-sig"))["R"]["Version"])' /tmp/scan-input/renv.lock)"
/tmp/install-r-runtime.sh --r-version "${R_VERSION}"
export PATH="/opt/R/${R_VERSION}/bin:${PATH}"

if [[ "${STAGE_INDEX}" != "1" ]]; then
  aws s3 cp "${CHECKPOINT_PREFIX}/latest/renv-cache.tar.gz" "${RUN_DIR}/checkpoint-renv-cache.tar.gz"
  aws s3 cp "${CHECKPOINT_PREFIX}/latest/renv-library.tar.gz" "${RUN_DIR}/checkpoint-renv-library.tar.gz"
  python /tmp/extract-archive.py --archive "${RUN_DIR}/checkpoint-renv-cache.tar.gz" --destination "${CACHE_DIR}"
  python /tmp/extract-archive.py --archive "${RUN_DIR}/checkpoint-renv-library.tar.gz" --destination "${LIBRARY_DIR}"
fi

printf '%s' "${STAGE_PACKAGES_JSON:-[]}" > "${RUN_DIR}/stage-packages.json"
python - <<'PY' "${RUN_DIR}/stage-packages.json" "${RUN_DIR}/stage-packages.txt"
import json
import sys
packages = json.load(open(sys.argv[1], encoding="utf-8"))
with open(sys.argv[2], "w", encoding="ascii") as fh:
    for package in packages:
        fh.write(f"{package}\n")
PY

Rscript /tmp/materialize-r-environment.R \
  --lock-file "${RUN_DIR}/renv.lock" \
  --project-dir "${PROJECT_DIR}" \
  --cache-dir "${CACHE_DIR}" \
  --library-dir "${LIBRARY_DIR}" \
  --output-dir "${RUN_DIR}" \
  --platform "${TARGET_PLATFORM}" \
  --packages-file "${RUN_DIR}/stage-packages.txt" \
  --clean false

python /tmp/bundle-directory.py --source-dir "${CACHE_DIR}" --output-file "${RUN_DIR}/checkpoint-renv-cache.tar.gz" --checksum-file "${RUN_DIR}/checkpoint-renv-cache.tar.gz.sha256"
python /tmp/bundle-directory.py --source-dir "${LIBRARY_DIR}" --output-file "${RUN_DIR}/checkpoint-renv-library.tar.gz" --checksum-file "${RUN_DIR}/checkpoint-renv-library.tar.gz.sha256"
aws s3 cp "${RUN_DIR}/checkpoint-renv-cache.tar.gz" "${CHECKPOINT_PREFIX}/latest/renv-cache.tar.gz"
aws s3 cp "${RUN_DIR}/checkpoint-renv-library.tar.gz" "${CHECKPOINT_PREFIX}/latest/renv-library.tar.gz"

cat > "${RUN_DIR}/stage-state.json" <<EOF
{"platform":"${TARGET_PLATFORM}","scan_execution_id":"${SCAN_EXECUTION_ID:-manual}","scan_timestamp":"${TS}","phase":"materialization","stage_index":${STAGE_INDEX},"total_stages":${TOTAL_STAGES},"final_stage":"${FINAL_STAGE}","checkpoint_prefix":"${CHECKPOINT_PREFIX#s3://}"}
EOF
aws s3 cp "${RUN_DIR}/stage-state.json" "${CHECKPOINT_PREFIX}/latest/stage-state.json"

if [[ "${FINAL_STAGE}" != "true" ]]; then
  aws s3 cp "${RUN_DIR}/restore.log" "${CHECKPOINT_PREFIX}/stages/${STAGE_INDEX}/restore.log"
  aws s3 cp "${RUN_DIR}/stage-packages.json" "${CHECKPOINT_PREFIX}/stages/${STAGE_INDEX}/packages.json"
  aws s3 cp "${RUN_DIR}/stage-state.json" "${CHECKPOINT_PREFIX}/stages/${STAGE_INDEX}/stage-state.json"
  exit 0
fi

LIBRARY_PATH="$(cat "${RUN_DIR}/library-path.txt")"
python /tmp/bundle-directory.py --source-dir "${CACHE_DIR}" --output-file "${RUN_DIR}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz" --checksum-file "${RUN_DIR}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
python /tmp/bundle-directory.py --source-dir "${LIBRARY_PATH}" --output-file "${RUN_DIR}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz" --checksum-file "${RUN_DIR}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
python /tmp/generate-r-materialization-summary.py --run-dir "${RUN_DIR}" --platform "${TARGET_PLATFORM}" --r-version "${R_VERSION}" --cache-dir "${CACHE_DIR}" --library-path "${LIBRARY_PATH}"
python /tmp/generate-r-sbom.py --installed-packages-file "${RUN_DIR}/installed-packages.csv" --out-file "${RUN_DIR}/r-packages.cdx.json"
python /tmp/scan-r-vulnerabilities.py --installed-packages-file "${RUN_DIR}/installed-packages.csv" --lock-file "${RUN_DIR}/renv.lock" --out-file "${RUN_DIR}/osv-report.json"
trivy sbom --format json --output "${RUN_DIR}/trivy-sbom-report.json" "${RUN_DIR}/r-packages.cdx.json" || true
GOVERNANCE_EXIT=0
python /tmp/generate-r-governance-artifacts.py --run-dir "${RUN_DIR}" --platform "${TARGET_PLATFORM}" --remediate-medium "${REMEDIATE_MEDIUM:-true}" --fail-on-medium "${FAIL_ON_MEDIUM:-false}" --remediate-unknown "${REMEDIATE_UNKNOWN:-true}" --fail-on-unknown "${FAIL_ON_UNKNOWN:-false}" || GOVERNANCE_EXIT=$?
tar -czf "${RUN_DIR}/environment-artifacts.tar.gz" -C "${RUN_DIR}" renv.lock installed-packages.csv session-info.txt renv-status.txt materialization-summary.json r-packages.cdx.json || true
aws s3 cp "${RUN_DIR}/" "s3://${EPHEMERAL_BUCKET}/${EPHEMERAL_PREFIX}/${TARGET_PLATFORM}/${TS}/" --recursive
aws s3 cp "${RUN_DIR}/renv.lock" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/r/${TARGET_PLATFORM}/${TS}/renv.lock"
if [[ -f "${RUN_DIR}/approval-candidate-packages.csv" ]]; then aws s3 cp "${RUN_DIR}/approval-candidate-packages.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/r/${TARGET_PLATFORM}/${TS}/approval-candidate-packages.csv"; fi
if [[ -f "${RUN_DIR}/installed-packages.csv" ]]; then aws s3 cp "${RUN_DIR}/installed-packages.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/r/${TARGET_PLATFORM}/${TS}/installed-packages.csv"; fi
if [[ -f "${RUN_DIR}/r-packages.cdx.json" ]]; then aws s3 cp "${RUN_DIR}/r-packages.cdx.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/r-packages.cdx.json"; fi
if [[ -f "${RUN_DIR}/session-info.txt" ]]; then aws s3 cp "${RUN_DIR}/session-info.txt" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/session-info.txt"; fi
if [[ -f "${RUN_DIR}/renv-status.txt" ]]; then aws s3 cp "${RUN_DIR}/renv-status.txt" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/renv-status.txt"; fi
if [[ -f "${RUN_DIR}/restore.log" ]]; then aws s3 cp "${RUN_DIR}/restore.log" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/restore.log"; fi
if [[ -f "${RUN_DIR}/materialization-summary.json" ]]; then aws s3 cp "${RUN_DIR}/materialization-summary.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/r/${TARGET_PLATFORM}/${TS}/materialization-summary.json"; fi
if [[ -f "${RUN_DIR}/environment-artifacts.tar.gz" ]]; then aws s3 cp "${RUN_DIR}/environment-artifacts.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/environment-artifacts.tar.gz"; fi
if [[ -f "${RUN_DIR}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz" ]]; then aws s3 cp "${RUN_DIR}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz"; fi
if [[ -f "${RUN_DIR}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz.sha256" ]]; then aws s3 cp "${RUN_DIR}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz.sha256" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/r/${TARGET_PLATFORM}/${TS}/renv-library-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"; fi
if [[ -f "${RUN_DIR}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz" ]]; then aws s3 cp "${RUN_DIR}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/packages/offline/r/${TARGET_PLATFORM}/${TS}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz"; fi
if [[ -f "${RUN_DIR}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz.sha256" ]]; then aws s3 cp "${RUN_DIR}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz.sha256" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/packages/offline/r/${TARGET_PLATFORM}/${TS}/renv-cache-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"; fi
if [[ -f "${RUN_DIR}/trivy-sbom-report.json" ]]; then aws s3 cp "${RUN_DIR}/trivy-sbom-report.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/model-results/r/${TARGET_PLATFORM}/${TS}/trivy-sbom-report.json"; fi
if [[ -f "${RUN_DIR}/osv-report.json" ]]; then aws s3 cp "${RUN_DIR}/osv-report.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/model-results/r/${TARGET_PLATFORM}/${TS}/osv-report.json"; fi
if [[ -f "${RUN_DIR}/vulnerability-findings.csv" ]]; then aws s3 cp "${RUN_DIR}/vulnerability-findings.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/r/${TARGET_PLATFORM}/${TS}/vulnerability-findings.csv"; fi
if [[ -f "${RUN_DIR}/remediation-required.csv" ]]; then aws s3 cp "${RUN_DIR}/remediation-required.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/r/${TARGET_PLATFORM}/${TS}/remediation-required.csv"; fi
if [[ -f "${RUN_DIR}/remediation-exceptions.csv" ]]; then aws s3 cp "${RUN_DIR}/remediation-exceptions.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/r/${TARGET_PLATFORM}/${TS}/remediation-exceptions.csv"; fi
if [[ -f "${RUN_DIR}/remediation-spreadsheet.csv" ]]; then aws s3 cp "${RUN_DIR}/remediation-spreadsheet.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/r/${TARGET_PLATFORM}/${TS}/remediation-spreadsheet.csv"; fi
if [[ -f "${RUN_DIR}/governance-summary.json" ]]; then aws s3 cp "${RUN_DIR}/governance-summary.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/r/${TARGET_PLATFORM}/${TS}/governance-summary.json"; fi
printf '{"platform":"%s","timestamp_utc":"%s","scan_execution_id":"%s","r_version":"%s","ephemeral_prefix":"%s/%s/%s","offline_bundle_prefix":"%s/packages/offline/r/%s/%s","cleanup":"requested","stage_index":"%s","total_stages":"%s"}' "${TARGET_PLATFORM}" "${TS}" "${SCAN_EXECUTION_ID:-manual}" "${R_VERSION}" "${EPHEMERAL_PREFIX}" "${TARGET_PLATFORM}" "${TS}" "${EVIDENCE_PREFIX}" "${TARGET_PLATFORM}" "${TS}" "${STAGE_INDEX}" "${TOTAL_STAGES}" > "${RUN_DIR}/run-metadata.json"
aws s3 cp "${RUN_DIR}/run-metadata.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/r/${TARGET_PLATFORM}/${TS}/run-metadata.json"
if [[ "${GOVERNANCE_EXIT}" -ne 0 ]]; then echo "Governance gate failed with exit ${GOVERNANCE_EXIT}"; exit "${GOVERNANCE_EXIT}"; fi
