#!/usr/bin/env bash
set -euo pipefail

TARGET_PLATFORM="${1:?platform argument required}"
TS="${SCAN_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_ID="${SCAN_EXECUTION_ID:-manual}"
EVIDENCE_RUN_SEGMENT="${TS}/${RUN_ID}"
BASE_DIR="${SCAN_WORK_DIR:-/var/lib/package-scanner}"
RUN_DIR="${BASE_DIR}/scan-out/${TS}-${RUN_ID}"
ROOT_PREFIX="${BASE_DIR}/micromamba"
ENV_PREFIX="${ROOT_PREFIX}/envs/target"
CHECKPOINT_PREFIX="s3://${EPHEMERAL_BUCKET}/${EPHEMERAL_PREFIX}/checkpoints/python/${RUN_ID}/${TARGET_PLATFORM}"
CHECKPOINT_INTERVAL_SECONDS="${CHECKPOINT_INTERVAL_SECONDS:-900}"
SCRIPT_ROOT="${SCRIPT_ROOT:-/opt/package-scanner/scripts}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
MAMBA_BIN="${MAMBA_BIN:-/usr/local/bin/micromamba}"
PYTHON_CPU_ONLY="${PYTHON_CPU_ONLY:-true}"
CONDA_PACK_BIN="${CONDA_PACK_BIN:-conda-pack}"
PYTHON_RESTORE_ENV_CHECKPOINT="${PYTHON_RESTORE_ENV_CHECKPOINT:-false}"
PYTHON_CHECKPOINT_INCLUDE_PKGS="${PYTHON_CHECKPOINT_INCLUDE_PKGS:-false}"
PYTHON_CHECKPOINT_INCLUDE_ENV="${PYTHON_CHECKPOINT_INCLUDE_ENV:-false}"
INPUT_TYPE="${INPUT_TYPE:-environment-yaml}"
MATERIALIZE_AFTER_SCAN="${MATERIALIZE_AFTER_SCAN:-true}"
PREFLIGHT_ONLY="${PREFLIGHT_ONLY:-false}"
CONDA_SUBDIR=""

if [[ -z "${CONDA_OVERRIDE_GLIBC:-}" ]] && command -v getconf >/dev/null 2>&1; then
  glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')"
  if [[ -n "${glibc_version}" ]]; then
    export CONDA_OVERRIDE_GLIBC="${glibc_version}"
  fi
fi

if [[ "${TARGET_PLATFORM}" == "linux-amd64" && -z "${CONDA_OVERRIDE_ARCHSPEC:-}" ]]; then
  export CONDA_OVERRIDE_ARCHSPEC="x86_64_v3"
fi

checkpoint_pid=""
CURRENT_STAGE="starting"

write_state() {
  local phase="$1"
  cat > "${RUN_DIR}/stage-state.json" <<EOF
{"platform":"${TARGET_PLATFORM}","scan_execution_id":"${RUN_ID}","scan_timestamp":"${TS}","phase":"${phase}","checkpoint_prefix":"${CHECKPOINT_PREFIX#s3://}"}
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

publish_preflight_artifacts() {
  local stage="$1"
  local source_dir="${RUN_DIR}/preflight-${stage}"
  local trace_prefix="s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}"
  local env_prefix="s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}"
  local model_prefix="s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/model-results/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}"
  local requirements_prefix="s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}"
  upload_if_exists "${source_dir}/preflight-summary.json" "${trace_prefix}/preflight-${stage}-summary.json"
  upload_if_exists "${source_dir}/conda-dry-run.json" "${trace_prefix}/preflight-${stage}-conda-dry-run.json"
  upload_if_exists "${source_dir}/resolved-packages.json" "${trace_prefix}/preflight-${stage}-resolved-packages.json"
  upload_if_exists "${source_dir}/installed-packages.json" "${trace_prefix}/preflight-${stage}-installed-packages.json"
  upload_if_exists "${source_dir}/installed-inventory-difference.json" "${trace_prefix}/preflight-${stage}-inventory-difference.json"
  upload_if_exists "${source_dir}/conda-resolved.cdx.json" "${env_prefix}/preflight-${stage}-conda-resolved.cdx.json"
  upload_if_exists "${source_dir}/installed-conda.cdx.json" "${env_prefix}/preflight-${stage}-installed-conda.cdx.json"
  upload_if_exists "${source_dir}/conda-${CONDA_SUBDIR}.explicit.txt" "${requirements_prefix}/preflight-${stage}-conda-${CONDA_SUBDIR}.explicit.txt"
  upload_if_exists "${source_dir}/osv-report.json" "${model_prefix}/preflight-${stage}-osv-report.json"
  upload_if_exists "${source_dir}/trivy-sbom-report.json" "${model_prefix}/preflight-${stage}-trivy-sbom-report.json"
  upload_if_exists "${source_dir}/installed-trivy-sbom-report.json" "${model_prefix}/preflight-${stage}-trivy-sbom-report.json"
}

render_requirements_environment() {
  local requirements_path="$1"
  local environment_path="$2"
  "${PYTHON_BIN}" - "${requirements_path}" "${environment_path}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

requirements_path = Path(sys.argv[1])
environment_path = Path(sys.argv[2])
packages: list[str] = []
for raw in requirements_path.read_text(encoding="utf-8", errors="ignore").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    packages.append(line)

payload = {
    "name": "target",
    "channels": ["conda-forge"],
    "dependencies": [
        "python",
        "pip",
        {
            "pip": packages,
        },
    ],
}
environment_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}

pack_python_env() {
  local output_file="$1"
  local checksum_file="$2"
  if [[ ! -d "${ENV_PREFIX}" ]]; then
    return 0
  fi
  "${CONDA_PACK_BIN}" \
    --prefix "${ENV_PREFIX}" \
    --output "${output_file}" \
    --format tar.gz \
    --ignore-missing-files \
    --force >/dev/null
  "${PYTHON_BIN}" - "${output_file}" "${checksum_file}" <<'PY'
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

archive = Path(sys.argv[1])
checksum_file = Path(sys.argv[2])
h = hashlib.sha256()
with archive.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        h.update(chunk)
checksum_file.write_text(f"{h.hexdigest()}  {archive.name}\n", encoding="utf-8")
PY
}

restore_packed_python_env() {
  local archive_path="$1"
  rm -rf "${ENV_PREFIX}"
  mkdir -p "${ENV_PREFIX}"
  "${PYTHON_BIN}" "${SCRIPT_ROOT}/extract-archive.py" --archive "${archive_path}" --destination "${ENV_PREFIX}"
  if [[ -x "${ENV_PREFIX}/bin/conda-unpack" ]]; then
    "${ENV_PREFIX}/bin/conda-unpack" >/dev/null
  elif [[ -x "${ENV_PREFIX}/Scripts/conda-unpack.exe" ]]; then
    "${ENV_PREFIX}/Scripts/conda-unpack.exe" >/dev/null
  else
    echo "conda-unpack was not found in restored environment ${ENV_PREFIX}" >&2
    exit 2
  fi
}

publish_checkpoint() {
  local phase="${1:-materialize}"
  publish_stage_state "${phase}"
  if [[ "${PYTHON_CHECKPOINT_INCLUDE_PKGS}" == "true" && -d "${ROOT_PREFIX}/pkgs" ]]; then
    "${PYTHON_BIN}" "${SCRIPT_ROOT}/bundle-directory.py" \
      --source-dir "${ROOT_PREFIX}/pkgs" \
      --output-file "${RUN_DIR}/checkpoint-python-pkgs.tar.gz" \
      --checksum-file "${RUN_DIR}/checkpoint-python-pkgs.tar.gz.sha256"
    aws s3 cp "${RUN_DIR}/checkpoint-python-pkgs.tar.gz" "${CHECKPOINT_PREFIX}/latest/python-pkgs.tar.gz" >/dev/null
    aws s3 cp "${RUN_DIR}/checkpoint-python-pkgs.tar.gz.sha256" "${CHECKPOINT_PREFIX}/latest/python-pkgs.tar.gz.sha256" >/dev/null
    rm -f "${RUN_DIR}/checkpoint-python-pkgs.tar.gz" "${RUN_DIR}/checkpoint-python-pkgs.tar.gz.sha256"
  fi
  if [[ "${PYTHON_CHECKPOINT_INCLUDE_ENV}" == "true" && -d "${ENV_PREFIX}" ]]; then
    pack_python_env "${RUN_DIR}/checkpoint-python-env.tar.gz" "${RUN_DIR}/checkpoint-python-env.tar.gz.sha256"
    aws s3 cp "${RUN_DIR}/checkpoint-python-env.tar.gz" "${CHECKPOINT_PREFIX}/latest/python-env.tar.gz" >/dev/null
    aws s3 cp "${RUN_DIR}/checkpoint-python-env.tar.gz.sha256" "${CHECKPOINT_PREFIX}/latest/python-env.tar.gz.sha256" >/dev/null
    rm -f "${RUN_DIR}/checkpoint-python-env.tar.gz" "${RUN_DIR}/checkpoint-python-env.tar.gz.sha256"
  fi
}

publish_stage_state() {
  local phase="$1"
  CURRENT_STAGE="${phase}"
  write_state "${phase}"
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
  local failed_phase="failed"
  if [[ "${CURRENT_STAGE}" == "preflight-plan" || "${CURRENT_STAGE}" == "preflight-installed" ]]; then
    failed_phase="${CURRENT_STAGE}-failed"
  fi
  write_state "${failed_phase}"
  upload_if_exists "${RUN_DIR}/restore.log" "${CHECKPOINT_PREFIX}/failures/restore.log"
  publish_preflight_artifacts plan || true
  publish_preflight_artifacts installed || true
  upload_if_exists "${RUN_DIR}/stage-state.json" "${CHECKPOINT_PREFIX}/failures/stage-state.json"
  publish_stage_state "${failed_phase}"
}
trap publish_failure_diagnostics ERR

mkdir -p "${RUN_DIR}" "${ROOT_PREFIX}" /tmp/scan-input
input_basename="$(basename "${INPUT_OBJECT_KEY}")"
aws s3 cp "s3://${INPUT_BUCKET}/${INPUT_OBJECT_KEY}" "/tmp/scan-input/${input_basename}" >/dev/null
REQUIREMENTS_ONLY_SCAN="false"
if [[ "${INPUT_TYPE}" == "requirements-lock" || "${INPUT_OBJECT_KEY}" == *.requirements.txt ]]; then
  REQUIREMENTS_ONLY_SCAN="true"
  cp "/tmp/scan-input/${input_basename}" "${RUN_DIR}/requirements.lock.txt"
  render_requirements_environment "${RUN_DIR}/requirements.lock.txt" "${RUN_DIR}/environment.yml"
else
  cp "/tmp/scan-input/${input_basename}" "${RUN_DIR}/environment.yml"
fi

if [[ "${REQUIREMENTS_ONLY_SCAN}" != "true" && "${PYTHON_CPU_ONLY}" == "true" ]]; then
  cp "${RUN_DIR}/environment.yml" "${RUN_DIR}/environment.original.yml"
  "${PYTHON_BIN}" - "${RUN_DIR}/environment.yml" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()

gpu_package_prefixes = (
    "cuda",
    "cudnn",
    "cudatoolkit",
    "cupy",
    "libcublas",
    "libcufft",
    "libcurand",
    "libcusolver",
    "libcusparse",
    "libnpp",
    "libnvjitlink",
    "libnvjpeg",
    "nccl",
    "nvidia-",
    "pytorch-cuda",
)
tensorflow_packages = {
    "tensorflow",
    "tensorflow-base",
    "tensorflow-estimator",
    "tensorflow-gpu",
}
dependency_re = re.compile(r"^(?P<indent>\s*)-\s+(?P<spec>[^#\s][^#]*?)(?P<comment>\s+#.*)?$")


def package_name(spec: str) -> str:
    return re.split(r"[=<>!~\s]", spec.strip(), maxsplit=1)[0].lower()


def canonical_package_name(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace(".", "-")


def cpu_tensorflow_spec(spec: str) -> str:
    parts = spec.strip().split("=")
    if len(parts) >= 2:
        return f"{parts[0]}={parts[1]}"
    return spec.strip().replace("tensorflow-gpu", "tensorflow")


sanitized: list[str] = []
removed: list[str] = []
rewritten: list[str] = []
conda_package_names: set[str] = set()
in_pip_section = False
pip_indent: int | None = None

for line in lines:
    match = dependency_re.match(line)
    if not match:
        continue
    indent = match.group("indent")
    spec = match.group("spec").strip()
    if spec == "pip:":
        in_pip_section = True
        pip_indent = len(indent)
        continue
    if in_pip_section and pip_indent is not None and len(indent) > pip_indent:
        continue
    in_pip_section = False
    name = package_name(spec)
    if name and name != "pip":
        conda_package_names.add(canonical_package_name(name))

in_pip_section = False
pip_indent = None

for line in lines:
    match = dependency_re.match(line)
    if not match:
        sanitized.append(line)
        continue

    indent = match.group("indent")
    spec = match.group("spec").strip()
    comment = match.group("comment") or ""
    name = package_name(spec)
    canonical_name = canonical_package_name(name)

    if spec == "pip:":
        in_pip_section = True
        pip_indent = len(indent)
        sanitized.append(line)
        continue

    in_nested_pip_dependency = in_pip_section and pip_indent is not None and len(indent) > pip_indent
    if not in_nested_pip_dependency:
        in_pip_section = False

    if in_nested_pip_dependency and canonical_name in conda_package_names:
        removed.append(f"{spec} (pip duplicate of conda package)")
        continue

    if name.startswith(gpu_package_prefixes):
        removed.append(spec)
        continue

    if name in tensorflow_packages and "cuda" in spec.lower():
        replacement = cpu_tensorflow_spec(spec)
        rewritten.append(f"{spec} -> {replacement}")
        sanitized.append(f"{indent}- {replacement}{comment}")
        continue

    sanitized.append(line)

path.write_text("\n".join(sanitized) + "\n", encoding="utf-8")
if removed or rewritten:
    log_path = path.with_name("environment.cpu-normalization.log")
    with log_path.open("w", encoding="utf-8") as handle:
        if removed:
            handle.write("Removed GPU-only dependencies:\n")
            for item in removed:
                handle.write(f"- {item}\n")
        if rewritten:
            handle.write("Rewritten GPU-pinned dependencies:\n")
            for item in rewritten:
                handle.write(f"- {item}\n")
PY
fi

case "${TARGET_PLATFORM}" in
  linux-amd64) CONDA_SUBDIR="linux-64" ;;
  linux-arm64) CONDA_SUBDIR="linux-aarch64" ;;
  *) echo "Conda preflight is unsupported for target platform: ${TARGET_PLATFORM}" >&2; exit 2 ;;
esac

# Gate before package archives are downloaded. This is deliberately skipped
# only for an analysis-only pip requirements scan, which has no Conda plan.
if [[ "${REQUIREMENTS_ONLY_SCAN}" != "true" || "${MATERIALIZE_AFTER_SCAN}" == "true" ]]; then
  publish_stage_state "preflight-plan"
  "${PYTHON_BIN}" "${SCRIPT_ROOT}/preflight-conda-environment.py" \
    --environment-file "${RUN_DIR}/environment.yml" \
    --out-dir "${RUN_DIR}/preflight-plan" \
    --conda-bin "${MAMBA_BIN}" \
    --trivy-bin trivy \
    --platform "${CONDA_SUBDIR}"
  publish_preflight_artifacts plan
  publish_stage_state "materialize"
fi

# A preflight review run intentionally stops before any package archive is
# downloaded or environment is linked.  It publishes the dry-resolved inventory
# and its vulnerability/governance evidence for Cyber approval.
if [[ "${PREFLIGHT_ONLY}" == "true" ]]; then
  cp "${RUN_DIR}/preflight-plan/resolved-packages.json" "${RUN_DIR}/conda-list.json"
  cp "${RUN_DIR}/preflight-plan/conda-resolved.cdx.json" "${RUN_DIR}/python-packages.cdx.json"
  cp "${RUN_DIR}/preflight-plan/trivy-sbom-report.json" "${RUN_DIR}/trivy-sbom-report.json"
  printf '[]\n' > "${RUN_DIR}/safety-report.json"
  publish_stage_state "governance"
  "${PYTHON_BIN}" "${SCRIPT_ROOT}/generate-governance-artifacts.py" \
    --run-dir "${RUN_DIR}" --platform "${TARGET_PLATFORM}" \
    --remediate-medium "${REMEDIATE_MEDIUM:-true}" --fail-on-medium "${FAIL_ON_MEDIUM:-false}"
  publish_stage_state "publish"
  aws s3 cp "${RUN_DIR}/" "s3://${EPHEMERAL_BUCKET}/${EPHEMERAL_PREFIX}/${TARGET_PLATFORM}/${TS}/" --recursive >/dev/null
  upload_if_exists "${RUN_DIR}/environment.yml" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/environment.yml"
  upload_if_exists "${RUN_DIR}/conda-list.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/conda-list.json"
  upload_if_exists "${RUN_DIR}/approval-candidate-packages.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/approval-candidate-packages.csv"
  upload_if_exists "${RUN_DIR}/trivy-sbom-report.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/model-results/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/trivy-sbom-report.json"
  for artifact in vulnerability-findings.csv remediation-required.csv remediation-exceptions.csv remediation-spreadsheet.csv; do
    upload_if_exists "${RUN_DIR}/${artifact}" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/${artifact}"
  done
  upload_if_exists "${RUN_DIR}/governance-summary.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/governance-summary.json"
  printf '{"platform":"%s","timestamp_utc":"%s","scan_execution_id":"%s","preflight_only":true,"materialize_after_scan":false}' "${TARGET_PLATFORM}" "${TS}" "${RUN_ID}" > "${RUN_DIR}/run-metadata.json"
  aws s3 cp "${RUN_DIR}/run-metadata.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/run-metadata.json" >/dev/null
  publish_stage_state "completed"
  exit 0
fi

if [[ "${REQUIREMENTS_ONLY_SCAN}" == "true" && "${MATERIALIZE_AFTER_SCAN}" != "true" ]]; then
  printf '[]\n' > "${RUN_DIR}/conda-list.json"
  : > "${RUN_DIR}/restore.log"
  PYTHON_VERSION="$("${PYTHON_BIN}" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
)"
else
  "${PYTHON_BIN}" "${SCRIPT_ROOT}/plan-python-environment-install.py" \
    --environment-file "${RUN_DIR}/environment.yml" \
    --run-dir "${RUN_DIR}"

  for planned_file in \
    "${RUN_DIR}/environment.conda-core.yml" \
    "${RUN_DIR}/environment.conda-native.yml" \
    "${RUN_DIR}/environment.conda-python.yml" \
    "${RUN_DIR}/environment.pip.requirements.txt" \
    "${RUN_DIR}/environment.install-plan.json"
  do
    if [[ ! -f "${planned_file}" ]]; then
      echo "planner-error: missing staged install artifact ${planned_file}" >&2
      exit 2
    fi
  done

  if aws s3 ls "${CHECKPOINT_PREFIX}/latest/python-pkgs.tar.gz" >/dev/null 2>&1; then
    aws s3 cp "${CHECKPOINT_PREFIX}/latest/python-pkgs.tar.gz" "${RUN_DIR}/checkpoint-python-pkgs.tar.gz" >/dev/null
    mkdir -p "${ROOT_PREFIX}/pkgs"
    "${PYTHON_BIN}" "${SCRIPT_ROOT}/extract-archive.py" --archive "${RUN_DIR}/checkpoint-python-pkgs.tar.gz" --destination "${ROOT_PREFIX}/pkgs"
    rm -f "${RUN_DIR}/checkpoint-python-pkgs.tar.gz"
  fi
  if [[ "${PYTHON_RESTORE_ENV_CHECKPOINT}" == "true" ]] && aws s3 ls "${CHECKPOINT_PREFIX}/latest/python-env.tar.gz" >/dev/null 2>&1; then
    aws s3 cp "${CHECKPOINT_PREFIX}/latest/python-env.tar.gz" "${RUN_DIR}/checkpoint-python-env.tar.gz" >/dev/null
    restore_packed_python_env "${RUN_DIR}/checkpoint-python-env.tar.gz"
    rm -f "${RUN_DIR}/checkpoint-python-env.tar.gz"
  fi

  publish_stage_state "materialize"
  start_checkpoint_loop

  if [[ -d "${ENV_PREFIX}" ]]; then
    "${MAMBA_BIN}" env update -r "${ROOT_PREFIX}" -n target -f "${RUN_DIR}/environment.conda-core.yml" > "${RUN_DIR}/restore.log" 2>&1
  else
    "${MAMBA_BIN}" create -r "${ROOT_PREFIX}" -y -n target -f "${RUN_DIR}/environment.conda-core.yml" > "${RUN_DIR}/restore.log" 2>&1
  fi

  if [[ -s "${RUN_DIR}/environment.conda-native.yml" ]]; then
    "${MAMBA_BIN}" env update -r "${ROOT_PREFIX}" -n target -f "${RUN_DIR}/environment.conda-native.yml" >> "${RUN_DIR}/restore.log" 2>&1
  fi

  if [[ -s "${RUN_DIR}/environment.conda-python.yml" ]]; then
    "${MAMBA_BIN}" env update -r "${ROOT_PREFIX}" -n target -f "${RUN_DIR}/environment.conda-python.yml" >> "${RUN_DIR}/restore.log" 2>&1
  fi

  if [[ -s "${RUN_DIR}/environment.pip.requirements.txt" ]]; then
    "${MAMBA_BIN}" run -r "${ROOT_PREFIX}" -n target python -m pip install --no-cache-dir --no-input -r "${RUN_DIR}/environment.pip.requirements.txt" >> "${RUN_DIR}/restore.log" 2>&1
  fi

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

  # Verify that the built prefix exactly matches a fresh target solve, and
  # scan the installed Conda inventory before it can be delivered.
  publish_stage_state "preflight-installed"
  "${PYTHON_BIN}" "${SCRIPT_ROOT}/preflight-conda-environment.py" \
    --environment-file "${RUN_DIR}/environment.yml" \
    --out-dir "${RUN_DIR}/preflight-installed" \
    --conda-bin "${MAMBA_BIN}" \
    --trivy-bin trivy \
    --platform "${CONDA_SUBDIR}" \
    --installed-prefix "${ENV_PREFIX}"
  publish_preflight_artifacts installed
fi
"${PYTHON_BIN}" "${SCRIPT_ROOT}/generate-python-materialization-summary.py" \
  --run-dir "${RUN_DIR}" \
  --platform "${TARGET_PLATFORM}" \
  --python-version "${PYTHON_VERSION}" \
  --root-prefix "${ROOT_PREFIX}" \
  --env-prefix "${ENV_PREFIX}" \
  --input-type "${INPUT_TYPE}"
MATERIALIZATION_VALIDATION_EXIT=0
if [[ "${REQUIREMENTS_ONLY_SCAN}" != "true" || "${MATERIALIZE_AFTER_SCAN}" == "true" ]]; then
  "${PYTHON_BIN}" - "${RUN_DIR}/materialization-summary.json" <<'PY' || MATERIALIZATION_VALIDATION_EXIT=$?
from __future__ import annotations

import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
missing_conda = summary.get("missing_requested_conda_packages") or []
missing_pip = summary.get("missing_requested_pip_packages") or []
if missing_conda or missing_pip:
    lines = []
    if missing_conda:
        lines.append("Missing requested conda packages:")
        lines.extend(f"- {item}" for item in missing_conda)
    if missing_pip:
        lines.append("Missing requested pip packages:")
        lines.extend(f"- {item}" for item in missing_pip)
    Path(sys.argv[1]).with_name("materialization-validation-error.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    raise SystemExit(4)
PY
fi

publish_stage_state "analysis"
"${PYTHON_BIN}" -m cyclonedx_py requirements "${RUN_DIR}/requirements.lock.txt" -o "${RUN_DIR}/python-packages.cdx.json" || true
trivy sbom --format json --output "${RUN_DIR}/trivy-sbom-report.json" "${RUN_DIR}/python-packages.cdx.json" || true
printf '[]\n' > "${RUN_DIR}/safety-report.json"
if [[ -n "${SAFETY_API_KEY:-}" ]]; then
  safety --key "${SAFETY_API_KEY}" scan --file "${RUN_DIR}/requirements.lock.txt" --output json > "${RUN_DIR}/safety-report.json" || true
fi
GOVERNANCE_EXIT=0
publish_stage_state "governance"
"${PYTHON_BIN}" "${SCRIPT_ROOT}/generate-governance-artifacts.py" \
  --run-dir "${RUN_DIR}" \
  --platform "${TARGET_PLATFORM}" \
  --remediate-medium "${REMEDIATE_MEDIUM:-true}" \
  --fail-on-medium "${FAIL_ON_MEDIUM:-false}" || GOVERNANCE_EXIT=$?

publish_stage_state "publish"
if [[ -d "${ROOT_PREFIX}/pkgs" ]]; then
  "${PYTHON_BIN}" "${SCRIPT_ROOT}/bundle-directory.py" \
    --source-dir "${ROOT_PREFIX}/pkgs" \
    --output-file "${RUN_DIR}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz" \
    --checksum-file "${RUN_DIR}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
fi
if [[ "${REQUIREMENTS_ONLY_SCAN}" != "true" || "${MATERIALIZE_AFTER_SCAN}" == "true" ]]; then
  pack_python_env "${RUN_DIR}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz" "${RUN_DIR}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
fi
environment_artifacts=(
  requirements.lock.txt
  python-packages.cdx.json
  materialization-summary.json
)
if [[ -f "${RUN_DIR}/environment.yml" ]]; then
  environment_artifacts=(environment.yml "${environment_artifacts[@]}")
fi
if [[ -f "${RUN_DIR}/conda-list.json" ]]; then
  environment_artifacts+=(conda-list.json)
fi
for optional_artifact in environment.original.yml environment.cpu-normalization.log; do
  if [[ -f "${RUN_DIR}/${optional_artifact}" ]]; then
    environment_artifacts+=("${optional_artifact}")
  fi
done
tar -czf "${RUN_DIR}/environment-artifacts.tar.gz" -C "${RUN_DIR}" "${environment_artifacts[@]}"

aws s3 cp "${RUN_DIR}/" "s3://${EPHEMERAL_BUCKET}/${EPHEMERAL_PREFIX}/${TARGET_PLATFORM}/${TS}/" --recursive >/dev/null
upload_if_exists "${RUN_DIR}/environment.yml" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/environment.yml"
upload_if_exists "${RUN_DIR}/environment.original.yml" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/environment.original.yml"
upload_if_exists "${RUN_DIR}/environment.cpu-normalization.log" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/environment.cpu-normalization.log"
upload_if_exists "${RUN_DIR}/requirements.lock.txt" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/requirements.lock.txt"
upload_if_exists "${RUN_DIR}/approval-candidate-packages.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/approval-candidate-packages.csv"
upload_if_exists "${RUN_DIR}/python-packages.cdx.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/python-packages.cdx.json"
upload_if_exists "${RUN_DIR}/conda-list.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/conda-list.json"
upload_if_exists "${RUN_DIR}/environment-artifacts.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/environment-artifacts.tar.gz"
upload_if_exists "${RUN_DIR}/restore.log" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/restore.log"
upload_if_exists "${RUN_DIR}/materialization-summary.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/materialization-summary.json"
upload_if_exists "${RUN_DIR}/materialization-validation-error.txt" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/materialization-validation-error.txt"
upload_if_exists "${RUN_DIR}/trivy-sbom-report.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/model-results/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/trivy-sbom-report.json"
upload_if_exists "${RUN_DIR}/safety-report.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/model-results/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/safety-report.json"
upload_if_exists "${RUN_DIR}/vulnerability-findings.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/vulnerability-findings.csv"
upload_if_exists "${RUN_DIR}/remediation-required.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/remediation-required.csv"
upload_if_exists "${RUN_DIR}/remediation-exceptions.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/remediation-exceptions.csv"
upload_if_exists "${RUN_DIR}/remediation-spreadsheet.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/governance/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/remediation-spreadsheet.csv"
upload_if_exists "${RUN_DIR}/governance-summary.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/governance-summary.json"
upload_if_exists "${RUN_DIR}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/packages/offline/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz"
upload_if_exists "${RUN_DIR}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz.sha256" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/packages/offline/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
upload_if_exists "${RUN_DIR}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz"
upload_if_exists "${RUN_DIR}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz.sha256" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
printf '{"platform":"%s","timestamp_utc":"%s","scan_execution_id":"%s","python_version":"%s","input_type":"%s","materialize_after_scan":"%s","ephemeral_prefix":"%s/%s/%s","offline_bundle_prefix":"%s/packages/offline/python/%s/%s","cleanup":"requested"}' "${TARGET_PLATFORM}" "${TS}" "${RUN_ID}" "${PYTHON_VERSION}" "${INPUT_TYPE}" "${MATERIALIZE_AFTER_SCAN}" "${EPHEMERAL_PREFIX}" "${TARGET_PLATFORM}" "${TS}" "${EVIDENCE_PREFIX}" "${TARGET_PLATFORM}" "${EVIDENCE_RUN_SEGMENT}" > "${RUN_DIR}/run-metadata.json"
aws s3 cp "${RUN_DIR}/run-metadata.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/run-metadata.json" >/dev/null
publish_stage_state "completed"

if [[ "${GOVERNANCE_EXIT}" -ne 0 ]]; then
  echo "Governance gate failed with exit ${GOVERNANCE_EXIT}" >&2
  exit "${GOVERNANCE_EXIT}"
fi
if [[ "${MATERIALIZATION_VALIDATION_EXIT}" -ne 0 ]]; then
  echo "Materialization validation failed with exit ${MATERIALIZATION_VALIDATION_EXIT}" >&2
  exit "${MATERIALIZATION_VALIDATION_EXIT}"
fi

exit 0
