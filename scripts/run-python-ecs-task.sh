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

checkpoint_pid=""

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
    pack_python_env "${RUN_DIR}/checkpoint-python-env.tar.gz" "${RUN_DIR}/checkpoint-python-env.tar.gz.sha256"
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

if [[ "${PYTHON_CPU_ONLY}" == "true" ]]; then
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

"${PYTHON_BIN}" - "${RUN_DIR}/environment.yml" "${RUN_DIR}" <<'PY'
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

env_path = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
data = yaml.safe_load(env_path.read_text(encoding="utf-8"))

channels = data.get("channels", [])
dependencies = data.get("dependencies", [])
name = data.get("name", "target")

conda_specs: list[str] = []
pip_specs: list[str] = []
for item in dependencies:
    if isinstance(item, str):
        conda_specs.append(item)
    elif isinstance(item, dict) and "pip" in item:
        pip_specs.extend(str(spec) for spec in item["pip"])

name_re = re.compile(r"[=<>!~\s]")


def package_name(spec: str) -> str:
    return name_re.split(spec.strip(), maxsplit=1)[0].lower()


core_names = {
    "python",
    "python_abi",
    "pip",
    "setuptools",
    "wheel",
    "ca-certificates",
    "openssl",
    "libffi",
    "libsqlite",
    "sqlite",
    "readline",
    "tk",
    "tzdata",
    "ncurses",
    "libzlib",
    "zlib",
    "zstd",
    "bzip2",
    "liblzma",
    "libgcc",
    "libgcc-ng",
    "libstdcxx",
    "libstdcxx-ng",
    "libgomp",
}

native_prefixes = (
    "lib",
    "perl",
    "r-",
    "bioconductor-",
    "xorg-",
    "font-",
    "fonts-",
    "cuda",
    "cudnn",
)
native_names = {
    "_openmp_mutex",
    "aragorn",
    "archspec",
    "backports.zstd",
    "bakta",
    "bbmap",
    "biopython",
    "blast",
    "brotli",
    "brotli-bin",
    "brotli-python",
    "c-ares",
    "cffi",
    "cgecore",
    "contourpy",
    "curl",
    "diamond",
    "entrez-direct",
    "fastqc",
    "fontconfig",
    "fonttools",
    "freetype",
    "git",
    "gperftools",
    "hmmer",
    "infernal",
    "isa-l",
    "kaleido-core",
    "keyutils",
    "kiwisolver",
    "kma",
    "kraken2",
    "krb5",
    "lcms2",
    "ld_impl_linux-64",
    "lerc",
    "lz4-c",
    "mathjax",
    "matplotlib-base",
    "multiqc",
    "ncbi-amrfinderplus",
    "ncbi-vdb",
    "networkx",
    "nspr",
    "nss",
    "numpy",
    "openjdk",
    "openjpeg",
    "pandas",
    "pbzip2",
    "pcre2",
    "pillow",
    "plotly",
    "popt",
    "psutil",
    "pthread-stubs",
    "pycparser",
    "pydantic-core",
    "pygments",
    "pyhmmer",
    "pyparsing",
    "pyrodigal",
    "pysocks",
    "python-isal",
    "python-kaleido",
    "python-zlib-ng",
    "qhull",
    "regex",
    "rpds-py",
    "rsync",
    "spectra",
    "sra-human-scrubber",
    "tabulate",
    "tar",
    "tiktoken",
    "tqdm",
    "trnascan-se",
    "unicodedata2",
    "urllib3",
    "virulencefinder",
    "wget",
    "xopen",
    "xxhash",
    "yaml",
    "zipp",
    "zlib-ng",
    "zstandard",
}

core_specs: list[str] = []
native_specs: list[str] = []
python_specs: list[str] = []

for spec in conda_specs:
    pkg = package_name(spec)
    if pkg in core_names:
      core_specs.append(spec)
    elif pkg.startswith(native_prefixes) or pkg in native_names:
      native_specs.append(spec)
    else:
      python_specs.append(spec)


def write_env(path: Path, specs: list[str]) -> None:
    payload = {"name": name, "channels": channels, "dependencies": specs}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


write_env(run_dir / "environment.conda-core.yml", core_specs)
write_env(run_dir / "environment.conda-native.yml", native_specs)
write_env(run_dir / "environment.conda-python.yml", python_specs)
(run_dir / "environment.pip.requirements.txt").write_text(
    "\n".join(pip_specs) + ("\n" if pip_specs else ""),
    encoding="utf-8",
)
(run_dir / "environment.install-plan.json").write_text(
    json.dumps(
        {
            "conda_core_count": len(core_specs),
            "conda_native_count": len(native_specs),
            "conda_python_count": len(python_specs),
            "pip_count": len(pip_specs),
            "conda_core": [package_name(spec) for spec in core_specs],
            "conda_native_sample": [package_name(spec) for spec in native_specs[:40]],
            "conda_python_sample": [package_name(spec) for spec in python_specs[:40]],
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY

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
fi
if aws s3 ls "${CHECKPOINT_PREFIX}/latest/python-env.tar.gz" >/dev/null 2>&1; then
  aws s3 cp "${CHECKPOINT_PREFIX}/latest/python-env.tar.gz" "${RUN_DIR}/checkpoint-python-env.tar.gz" >/dev/null
  restore_packed_python_env "${RUN_DIR}/checkpoint-python-env.tar.gz"
fi

write_state "materialize"
start_checkpoint_loop

{
  if [[ -d "${ENV_PREFIX}" ]]; then
    "${MAMBA_BIN}" env update -r "${ROOT_PREFIX}" -n target -f "${RUN_DIR}/environment.conda-core.yml"
  else
    "${MAMBA_BIN}" create -r "${ROOT_PREFIX}" -y -n target -f "${RUN_DIR}/environment.conda-core.yml"
  fi

  if [[ -s "${RUN_DIR}/environment.conda-native.yml" ]]; then
    "${MAMBA_BIN}" env update -r "${ROOT_PREFIX}" -n target -f "${RUN_DIR}/environment.conda-native.yml"
  fi

  if [[ -s "${RUN_DIR}/environment.conda-python.yml" ]]; then
    "${MAMBA_BIN}" env update -r "${ROOT_PREFIX}" -n target -f "${RUN_DIR}/environment.conda-python.yml"
  fi

  if [[ -s "${RUN_DIR}/environment.pip.requirements.txt" ]]; then
    "${MAMBA_BIN}" run -r "${ROOT_PREFIX}" -n target python -m pip install --no-input -r "${RUN_DIR}/environment.pip.requirements.txt"
  fi
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

write_state "analysis"
"${PYTHON_BIN}" -m cyclonedx_py requirements "${RUN_DIR}/requirements.lock.txt" -o "${RUN_DIR}/python-packages.cdx.json" || true
trivy sbom --format json --output "${RUN_DIR}/trivy-sbom-report.json" "${RUN_DIR}/python-packages.cdx.json" || true
printf '[]\n' > "${RUN_DIR}/safety-report.json"
if [[ -n "${SAFETY_API_KEY:-}" ]]; then
  safety --key "${SAFETY_API_KEY}" scan --file "${RUN_DIR}/requirements.lock.txt" --output json > "${RUN_DIR}/safety-report.json" || true
fi
GOVERNANCE_EXIT=0
write_state "governance"
"${PYTHON_BIN}" "${SCRIPT_ROOT}/generate-governance-artifacts.py" \
  --run-dir "${RUN_DIR}" \
  --platform "${TARGET_PLATFORM}" \
  --remediate-medium "${REMEDIATE_MEDIUM:-true}" \
  --fail-on-medium "${FAIL_ON_MEDIUM:-false}" || GOVERNANCE_EXIT=$?

write_state "publish"
"${PYTHON_BIN}" "${SCRIPT_ROOT}/bundle-directory.py" \
  --source-dir "${ROOT_PREFIX}/pkgs" \
  --output-file "${RUN_DIR}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz" \
  --checksum-file "${RUN_DIR}/python-pkgs-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
pack_python_env "${RUN_DIR}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz" "${RUN_DIR}/python-env-${TARGET_PLATFORM}-${TS}.tar.gz.sha256"
tar -czf "${RUN_DIR}/environment-artifacts.tar.gz" -C "${RUN_DIR}" environment.yml requirements.lock.txt conda-list.json python-packages.cdx.json materialization-summary.json environment.original.yml environment.cpu-normalization.log || true

aws s3 cp "${RUN_DIR}/" "s3://${EPHEMERAL_BUCKET}/${EPHEMERAL_PREFIX}/${TARGET_PLATFORM}/${TS}/" --recursive >/dev/null
aws s3 cp "${RUN_DIR}/environment.yml" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/environment.yml" >/dev/null
upload_if_exists "${RUN_DIR}/environment.original.yml" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/environment.original.yml"
upload_if_exists "${RUN_DIR}/environment.cpu-normalization.log" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/environment.cpu-normalization.log"
upload_if_exists "${RUN_DIR}/requirements.lock.txt" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/requirements.lock.txt"
upload_if_exists "${RUN_DIR}/approval-candidate-packages.csv" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/requirements/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/approval-candidate-packages.csv"
upload_if_exists "${RUN_DIR}/python-packages.cdx.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/python-packages.cdx.json"
upload_if_exists "${RUN_DIR}/conda-list.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/conda-list.json"
upload_if_exists "${RUN_DIR}/environment-artifacts.tar.gz" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/environment-artifacts.tar.gz"
upload_if_exists "${RUN_DIR}/restore.log" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/env-artifacts/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/restore.log"
upload_if_exists "${RUN_DIR}/materialization-summary.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/materialization-summary.json"
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
printf '{"platform":"%s","timestamp_utc":"%s","scan_execution_id":"%s","python_version":"%s","ephemeral_prefix":"%s/%s/%s","offline_bundle_prefix":"%s/packages/offline/python/%s/%s","cleanup":"requested"}' "${TARGET_PLATFORM}" "${TS}" "${RUN_ID}" "${PYTHON_VERSION}" "${EPHEMERAL_PREFIX}" "${TARGET_PLATFORM}" "${TS}" "${EVIDENCE_PREFIX}" "${TARGET_PLATFORM}" "${EVIDENCE_RUN_SEGMENT}" > "${RUN_DIR}/run-metadata.json"
aws s3 cp "${RUN_DIR}/run-metadata.json" "s3://${EVIDENCE_BUCKET}/${EVIDENCE_PREFIX}/traceability/python/${TARGET_PLATFORM}/${EVIDENCE_RUN_SEGMENT}/run-metadata.json" >/dev/null
write_state "completed"
aws s3 cp "${RUN_DIR}/stage-state.json" "${CHECKPOINT_PREFIX}/latest/stage-state.json" >/dev/null

if [[ "${GOVERNANCE_EXIT}" -ne 0 ]]; then
  echo "Governance gate failed with exit ${GOVERNANCE_EXIT}" >&2
  exit "${GOVERNANCE_EXIT}"
fi

exit 0
