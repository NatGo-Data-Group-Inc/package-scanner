param(
  [Parameter(Mandatory = $true)]
  [string]$Platform
)

$ErrorActionPreference = 'Stop'

$ts = if ($env:SCAN_TIMESTAMP) { $env:SCAN_TIMESTAMP } else { (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') }
$runId = if ($env:SCAN_EXECUTION_ID) { $env:SCAN_EXECUTION_ID } else { 'manual' }
$evidenceRunSegment = "$ts/$runId"
$baseDir = if ($env:SCAN_WORK_DIR) { $env:SCAN_WORK_DIR } else { 'C:\package-scanner-data' }
$runDir = Join-Path $baseDir "scan-out\$ts-$runId"
$rootPrefix = Join-Path $baseDir 'micromamba'
$envPrefix = Join-Path $rootPrefix 'envs\target'
$checkpointPrefix = "s3://${env:EPHEMERAL_BUCKET}/${env:EPHEMERAL_PREFIX}/checkpoints/python/$runId/$Platform"
$checkpointIntervalSeconds = if ($env:CHECKPOINT_INTERVAL_SECONDS) { [int]$env:CHECKPOINT_INTERVAL_SECONDS } else { 900 }
$scriptRoot = if ($env:SCRIPT_ROOT) { $env:SCRIPT_ROOT } else { 'C:\package-scanner\scripts' }
$pythonBin = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { 'python' }
$mambaBin = if ($env:MAMBA_BIN) { $env:MAMBA_BIN } else { 'C:\micromamba\Library\bin\micromamba.exe' }
$condaPackBin = if ($env:CONDA_PACK_BIN) { $env:CONDA_PACK_BIN } else { 'conda-pack' }
$pythonCpuOnly = if ($env:PYTHON_CPU_ONLY) { $env:PYTHON_CPU_ONLY } else { 'true' }
$pythonRestoreEnvCheckpoint = if ($env:PYTHON_RESTORE_ENV_CHECKPOINT) { $env:PYTHON_RESTORE_ENV_CHECKPOINT } else { 'false' }
$pythonCheckpointIncludePkgs = if ($env:PYTHON_CHECKPOINT_INCLUDE_PKGS) { $env:PYTHON_CHECKPOINT_INCLUDE_PKGS } else { 'false' }
$pythonCheckpointIncludeEnv = if ($env:PYTHON_CHECKPOINT_INCLUDE_ENV) { $env:PYTHON_CHECKPOINT_INCLUDE_ENV } else { 'false' }
$inputType = if ($env:INPUT_TYPE) { $env:INPUT_TYPE } else { 'environment-yaml' }
$materializeAfterScan = if ($env:MATERIALIZE_AFTER_SCAN) { $env:MATERIALIZE_AFTER_SCAN } else { 'true' }
$requirementsOnlyScan = ($inputType -eq 'requirements-lock')
$checkpointJob = $null

function Write-State {
  param([string]$Phase)
  $payload = "{`"platform`":`"$Platform`",`"scan_execution_id`":`"$runId`",`"scan_timestamp`":`"$ts`",`"phase`":`"$Phase`",`"checkpoint_prefix`":`"$($checkpointPrefix.Replace('s3://', ''))`"}"
  $payload | Out-File "$runDir\stage-state.json" -Encoding ascii
}

function Upload-IfExists {
  param([string]$Path, [string]$Destination)
  if (Test-Path $Path) {
    aws s3 cp $Path $Destination | Out-Null
  }
}

function Write-Checksum {
  param([string]$Path, [string]$ChecksumFile)
  $hash = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLower()
  "$hash  $([System.IO.Path]::GetFileName($Path))" | Out-File $ChecksumFile -Encoding ascii
}

function Pack-PythonEnv {
  param([string]$OutputFile, [string]$ChecksumFile)
  if (-not (Test-Path $envPrefix)) { return }
  & $condaPackBin --prefix $envPrefix --output $OutputFile --format tar.gz --ignore-missing-files --force | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "conda-pack failed" }
  Write-Checksum -Path $OutputFile -ChecksumFile $ChecksumFile
}

function Restore-PackedPythonEnv {
  param([string]$ArchivePath)
  if (Test-Path $envPrefix) {
    Remove-Item $envPrefix -Recurse -Force
  }
  New-Item -ItemType Directory -Force -Path $envPrefix | Out-Null
  & $pythonBin "$scriptRoot\extract-archive.py" --archive $ArchivePath --destination $envPrefix
  if ($LASTEXITCODE -ne 0) { throw "restore packed env failed" }
  $condaUnpack = Join-Path $envPrefix 'Scripts\conda-unpack.exe'
  if (-not (Test-Path $condaUnpack)) {
    throw "conda-unpack was not found in restored environment $envPrefix"
  }
  & $condaUnpack | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "conda-unpack failed" }
}

function Publish-Checkpoint {
  param([string]$Phase = 'restore')
  Write-State -Phase $Phase
  $pkgsDir = Join-Path $rootPrefix 'pkgs'
  if ($pythonCheckpointIncludePkgs -eq 'true' -and (Test-Path $pkgsDir)) {
    & $pythonBin "$scriptRoot\bundle-directory.py" --source-dir $pkgsDir --output-file "$runDir\checkpoint-python-pkgs.tar.gz" --checksum-file "$runDir\checkpoint-python-pkgs.tar.gz.sha256"
    aws s3 cp "$runDir\checkpoint-python-pkgs.tar.gz" "$checkpointPrefix/latest/python-pkgs.tar.gz" | Out-Null
    aws s3 cp "$runDir\checkpoint-python-pkgs.tar.gz.sha256" "$checkpointPrefix/latest/python-pkgs.tar.gz.sha256" | Out-Null
    Remove-Item "$runDir\checkpoint-python-pkgs.tar.gz", "$runDir\checkpoint-python-pkgs.tar.gz.sha256" -Force -ErrorAction SilentlyContinue
  }
  if ($pythonCheckpointIncludeEnv -eq 'true' -and (Test-Path $envPrefix)) {
    Pack-PythonEnv "$runDir\checkpoint-python-env.tar.gz" "$runDir\checkpoint-python-env.tar.gz.sha256"
    aws s3 cp "$runDir\checkpoint-python-env.tar.gz" "$checkpointPrefix/latest/python-env.tar.gz" | Out-Null
    aws s3 cp "$runDir\checkpoint-python-env.tar.gz.sha256" "$checkpointPrefix/latest/python-env.tar.gz.sha256" | Out-Null
    Remove-Item "$runDir\checkpoint-python-env.tar.gz", "$runDir\checkpoint-python-env.tar.gz.sha256" -Force -ErrorAction SilentlyContinue
  }
  aws s3 cp "$runDir\stage-state.json" "$checkpointPrefix/latest/stage-state.json" | Out-Null
}

function Stop-CheckpointLoop {
  if ($checkpointJob) {
    Stop-Job $checkpointJob -ErrorAction SilentlyContinue | Out-Null
    Remove-Job $checkpointJob -Force -ErrorAction SilentlyContinue | Out-Null
    $script:checkpointJob = $null
  }
}

function Start-CheckpointLoop {
  $script:checkpointJob = Start-Job -ScriptBlock {
    param($CheckpointIntervalSeconds, $runDir, $rootPrefix, $envPrefix, $checkpointPrefix, $Platform, $runId, $ts, $scriptRoot, $pythonBin, $condaPackBin, $pythonCheckpointIncludePkgs, $pythonCheckpointIncludeEnv)
    function Write-StateInner {
      param([string]$Phase)
      $payload = "{`"platform`":`"$Platform`",`"scan_execution_id`":`"$runId`",`"scan_timestamp`":`"$ts`",`"phase`":`"$Phase`",`"checkpoint_prefix`":`"$($checkpointPrefix.Replace('s3://', ''))`"}"
      $payload | Out-File "$runDir\stage-state.json" -Encoding ascii
    }
    function Write-ChecksumInner {
      param([string]$Path, [string]$ChecksumFile)
      $hash = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLower()
      "$hash  $([System.IO.Path]::GetFileName($Path))" | Out-File $ChecksumFile -Encoding ascii
    }
    while ($true) {
      Start-Sleep -Seconds $CheckpointIntervalSeconds
      try {
        Write-StateInner -Phase 'restore'
        $pkgsDir = Join-Path $rootPrefix 'pkgs'
        if ($pythonCheckpointIncludePkgs -eq 'true' -and (Test-Path $pkgsDir)) {
          & $pythonBin "$scriptRoot\bundle-directory.py" --source-dir $pkgsDir --output-file "$runDir\checkpoint-python-pkgs.tar.gz" --checksum-file "$runDir\checkpoint-python-pkgs.tar.gz.sha256"
          aws s3 cp "$runDir\checkpoint-python-pkgs.tar.gz" "$checkpointPrefix/latest/python-pkgs.tar.gz" | Out-Null
          aws s3 cp "$runDir\checkpoint-python-pkgs.tar.gz.sha256" "$checkpointPrefix/latest/python-pkgs.tar.gz.sha256" | Out-Null
          Remove-Item "$runDir\checkpoint-python-pkgs.tar.gz", "$runDir\checkpoint-python-pkgs.tar.gz.sha256" -Force -ErrorAction SilentlyContinue
        }
        if ($pythonCheckpointIncludeEnv -eq 'true' -and (Test-Path $envPrefix)) {
          & $condaPackBin --prefix $envPrefix --output "$runDir\checkpoint-python-env.tar.gz" --format tar.gz --ignore-missing-files --force | Out-Null
          Write-ChecksumInner "$runDir\checkpoint-python-env.tar.gz" "$runDir\checkpoint-python-env.tar.gz.sha256"
          aws s3 cp "$runDir\checkpoint-python-env.tar.gz" "$checkpointPrefix/latest/python-env.tar.gz" | Out-Null
          aws s3 cp "$runDir\checkpoint-python-env.tar.gz.sha256" "$checkpointPrefix/latest/python-env.tar.gz.sha256" | Out-Null
          Remove-Item "$runDir\checkpoint-python-env.tar.gz", "$runDir\checkpoint-python-env.tar.gz.sha256" -Force -ErrorAction SilentlyContinue
        }
        aws s3 cp "$runDir\stage-state.json" "$checkpointPrefix/latest/stage-state.json" | Out-Null
      } catch {}
    }
  } -ArgumentList $checkpointIntervalSeconds, $runDir, $rootPrefix, $envPrefix, $checkpointPrefix, $Platform, $runId, $ts, $scriptRoot, $pythonBin, $condaPackBin, $pythonCheckpointIncludePkgs, $pythonCheckpointIncludeEnv
}

function Render-RequirementsEnvironment {
  param([string]$RequirementsPath, [string]$EnvironmentPath)
  & $pythonBin -c @'
from __future__ import annotations

import json
import sys
from pathlib import Path

requirements_path = Path(sys.argv[1])
environment_path = Path(sys.argv[2])
packages = []
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
'@ $RequirementsPath $EnvironmentPath
  if ($LASTEXITCODE -ne 0) { throw "requirements render failed" }
}

try {
  New-Item -ItemType Directory -Force -Path $runDir, 'C:\scan-input', $rootPrefix | Out-Null
  $inputFile = Join-Path 'C:\scan-input' ([System.IO.Path]::GetFileName($env:INPUT_OBJECT_KEY))
  aws s3 cp "s3://${env:INPUT_BUCKET}/${env:INPUT_OBJECT_KEY}" $inputFile | Out-Null

  if ($requirementsOnlyScan) {
    Copy-Item $inputFile "$runDir\requirements.lock.txt" -Force
    Render-RequirementsEnvironment "$runDir\requirements.lock.txt" "$runDir\environment.yml"
  } else {
    Copy-Item $inputFile "$runDir\environment.yml" -Force
  }

  if (($requirementsOnlyScan -eq $false) -and ($pythonCpuOnly -eq 'true')) {
    Copy-Item "$runDir\environment.yml" "$runDir\environment.original.yml" -Force
    $cpuNormalizationScriptPath = Join-Path $runDir 'cpu-normalize-environment.py'
    $cpuNormalizationScript = @'
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

sanitized = []
removed = []
rewritten = []
conda_package_names = set()
in_pip_section = False
pip_indent = None

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
'@
    $cpuNormalizationScript | Out-File $cpuNormalizationScriptPath -Encoding ascii
    & $pythonBin $cpuNormalizationScriptPath "$runDir\environment.yml"
    if ($LASTEXITCODE -ne 0) { throw "cpu normalization failed" }
  }

  if ($requirementsOnlyScan -and $materializeAfterScan -ne 'true') {
    '[]' | Out-File "$runDir\conda-list.json" -Encoding ascii
    '' | Out-File "$runDir\restore.log" -Encoding ascii
    $pythonVersion = & $pythonBin -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
  } else {
    & $pythonBin "$scriptRoot\plan-python-environment-install.py" --environment-file "$runDir\environment.yml" --run-dir $runDir
    if ($LASTEXITCODE -ne 0) { throw "planner failed" }

    foreach ($planned in @(
      "$runDir\environment.conda-core.yml",
      "$runDir\environment.conda-native.yml",
      "$runDir\environment.conda-python.yml",
      "$runDir\environment.pip.requirements.txt",
      "$runDir\environment.install-plan.json"
    )) {
      if (-not (Test-Path $planned)) { throw "planner-error: missing staged install artifact $planned" }
    }

    try {
      aws s3 cp "$checkpointPrefix/latest/python-pkgs.tar.gz" "$runDir\checkpoint-python-pkgs.tar.gz" | Out-Null
      if (Test-Path "$runDir\checkpoint-python-pkgs.tar.gz") {
        $pkgsDir = Join-Path $rootPrefix 'pkgs'
        New-Item -ItemType Directory -Force -Path $pkgsDir | Out-Null
        & $pythonBin "$scriptRoot\extract-archive.py" --archive "$runDir\checkpoint-python-pkgs.tar.gz" --destination $pkgsDir
        Remove-Item "$runDir\checkpoint-python-pkgs.tar.gz" -Force -ErrorAction SilentlyContinue
      }
    } catch {}

    if ($pythonRestoreEnvCheckpoint -eq 'true') {
      try {
        aws s3 cp "$checkpointPrefix/latest/python-env.tar.gz" "$runDir\checkpoint-python-env.tar.gz" | Out-Null
        if (Test-Path "$runDir\checkpoint-python-env.tar.gz") {
          Restore-PackedPythonEnv "$runDir\checkpoint-python-env.tar.gz"
          Remove-Item "$runDir\checkpoint-python-env.tar.gz" -Force -ErrorAction SilentlyContinue
        }
      } catch {}
    }

    Write-State -Phase 'materialize'
    Start-CheckpointLoop

    & {
      if (Test-Path $envPrefix) {
        & $mambaBin env update -r $rootPrefix -n target -f "$runDir\environment.conda-core.yml"
      } else {
        & $mambaBin create -r $rootPrefix -y -n target -f "$runDir\environment.conda-core.yml"
      }
      if ($LASTEXITCODE -ne 0) { throw "conda core stage failed" }

      if ((Get-Item "$runDir\environment.conda-native.yml").Length -gt 0) {
        & $mambaBin env update -r $rootPrefix -n target -f "$runDir\environment.conda-native.yml"
        if ($LASTEXITCODE -ne 0) { throw "conda native stage failed" }
      }

      if ((Get-Item "$runDir\environment.conda-python.yml").Length -gt 0) {
        & $mambaBin env update -r $rootPrefix -n target -f "$runDir\environment.conda-python.yml"
        if ($LASTEXITCODE -ne 0) { throw "conda python stage failed" }
      }

      if ((Get-Item "$runDir\environment.pip.requirements.txt").Length -gt 0) {
        & $mambaBin run -r $rootPrefix -n target python -m pip install --no-cache-dir --no-input -r "$runDir\environment.pip.requirements.txt"
        if ($LASTEXITCODE -ne 0) { throw "pip stage failed" }
      }
    } *>&1 | Tee-Object -FilePath "$runDir\restore.log"

    Stop-CheckpointLoop
    Publish-Checkpoint -Phase 'restored'

    $pythonVersion = & $mambaBin run -r $rootPrefix -n target python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
    & $mambaBin run -r $rootPrefix -n target python -m pip list --format=freeze | Out-File "$runDir\requirements.lock.txt" -Encoding ascii
    & $mambaBin list -r $rootPrefix -n target --json | Out-File "$runDir\conda-list.json" -Encoding ascii
    $envPrefix | Out-File "$runDir\env-prefix.txt" -Encoding ascii
  }

  & $pythonBin "$scriptRoot\generate-python-materialization-summary.py" --run-dir $runDir --platform $Platform --python-version $pythonVersion --root-prefix $rootPrefix --env-prefix $envPrefix --input-type $inputType
  $materializationValidationExit = 0
  if ((-not $requirementsOnlyScan) -or $materializeAfterScan -eq 'true') {
    & $pythonBin -c @'
from __future__ import annotations

import json
import sys
from pathlib import Path

summary_path = Path(sys.argv[1])
summary = json.loads(summary_path.read_text(encoding="utf-8"))
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
    summary_path.with_name("materialization-validation-error.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    raise SystemExit(4)
'@ "$runDir\materialization-summary.json"
    if ($LASTEXITCODE -ne 0) { $materializationValidationExit = $LASTEXITCODE }
  }

  Write-State -Phase 'analysis'
  try {
    & $pythonBin -m cyclonedx_py requirements "$runDir\requirements.lock.txt" -o "$runDir\python-packages.cdx.json" 2>$null
  } catch {}
  try {
    & C:\trivy\trivy.exe sbom --format json --output "$runDir\trivy-sbom-report.json" "$runDir\python-packages.cdx.json" 2>$null
  } catch {}
  '[]' | Out-File "$runDir\safety-report.json" -Encoding ascii
  if ($env:SAFETY_API_KEY) {
    try {
      & safety --key $env:SAFETY_API_KEY scan --file "$runDir\requirements.lock.txt" --output json | Out-File "$runDir\safety-report.json" -Encoding ascii
    } catch {}
  }

  $govExit = 0
  Write-State -Phase 'governance'
  & $pythonBin "$scriptRoot\generate-governance-artifacts.py" --run-dir $runDir --platform $Platform --remediate-medium $env:REMEDIATE_MEDIUM --fail-on-medium $env:FAIL_ON_MEDIUM
  if ($LASTEXITCODE -ne 0) { $govExit = $LASTEXITCODE }

  Write-State -Phase 'publish'
  $pkgsDir = Join-Path $rootPrefix 'pkgs'
  if (Test-Path $pkgsDir) {
    & $pythonBin "$scriptRoot\bundle-directory.py" --source-dir $pkgsDir --output-file "$runDir\python-pkgs-$Platform-$ts.tar.gz" --checksum-file "$runDir\python-pkgs-$Platform-$ts.tar.gz.sha256"
  }
  if ((-not $requirementsOnlyScan) -or $materializeAfterScan -eq 'true') {
    Pack-PythonEnv "$runDir\python-env-$Platform-$ts.tar.gz" "$runDir\python-env-$Platform-$ts.tar.gz.sha256"
  }

  $environmentArtifacts = @('requirements.lock.txt', 'python-packages.cdx.json', 'materialization-summary.json')
  if (Test-Path "$runDir\environment.yml") { $environmentArtifacts = @('environment.yml') + $environmentArtifacts }
  if (Test-Path "$runDir\conda-list.json") { $environmentArtifacts += 'conda-list.json' }
  foreach ($optional in @('environment.original.yml', 'environment.cpu-normalization.log')) {
    if (Test-Path (Join-Path $runDir $optional)) { $environmentArtifacts += $optional }
  }
  try {
    tar -czf "$runDir\environment-artifacts.tar.gz" -C $runDir @environmentArtifacts
  } catch {}

  aws s3 cp "$runDir\" "s3://${env:EPHEMERAL_BUCKET}/${env:EPHEMERAL_PREFIX}/$Platform/$ts/" --recursive | Out-Null
  Upload-IfExists "$runDir\environment.yml" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/requirements/python/$Platform/$evidenceRunSegment/environment.yml"
  Upload-IfExists "$runDir\environment.original.yml" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/requirements/python/$Platform/$evidenceRunSegment/environment.original.yml"
  Upload-IfExists "$runDir\environment.cpu-normalization.log" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/requirements/python/$Platform/$evidenceRunSegment/environment.cpu-normalization.log"
  Upload-IfExists "$runDir\requirements.lock.txt" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/requirements/python/$Platform/$evidenceRunSegment/requirements.lock.txt"
  Upload-IfExists "$runDir\approval-candidate-packages.csv" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/requirements/python/$Platform/$evidenceRunSegment/approval-candidate-packages.csv"
  Upload-IfExists "$runDir\python-packages.cdx.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/python/$Platform/$evidenceRunSegment/python-packages.cdx.json"
  Upload-IfExists "$runDir\conda-list.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/python/$Platform/$evidenceRunSegment/conda-list.json"
  Upload-IfExists "$runDir\environment-artifacts.tar.gz" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/python/$Platform/$evidenceRunSegment/environment-artifacts.tar.gz"
  Upload-IfExists "$runDir\restore.log" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/python/$Platform/$evidenceRunSegment/restore.log"
  Upload-IfExists "$runDir\materialization-summary.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/traceability/python/$Platform/$evidenceRunSegment/materialization-summary.json"
  Upload-IfExists "$runDir\materialization-validation-error.txt" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/traceability/python/$Platform/$evidenceRunSegment/materialization-validation-error.txt"
  Upload-IfExists "$runDir\trivy-sbom-report.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/model-results/python/$Platform/$evidenceRunSegment/trivy-sbom-report.json"
  Upload-IfExists "$runDir\safety-report.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/model-results/python/$Platform/$evidenceRunSegment/safety-report.json"
  Upload-IfExists "$runDir\vulnerability-findings.csv" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/governance/python/$Platform/$evidenceRunSegment/vulnerability-findings.csv"
  Upload-IfExists "$runDir\remediation-required.csv" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/governance/python/$Platform/$evidenceRunSegment/remediation-required.csv"
  Upload-IfExists "$runDir\remediation-exceptions.csv" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/governance/python/$Platform/$evidenceRunSegment/remediation-exceptions.csv"
  Upload-IfExists "$runDir\remediation-spreadsheet.csv" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/governance/python/$Platform/$evidenceRunSegment/remediation-spreadsheet.csv"
  Upload-IfExists "$runDir\governance-summary.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/traceability/python/$Platform/$evidenceRunSegment/governance-summary.json"
  Upload-IfExists "$runDir\python-pkgs-$Platform-$ts.tar.gz" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/packages/offline/python/$Platform/$evidenceRunSegment/python-pkgs-$Platform-$ts.tar.gz"
  Upload-IfExists "$runDir\python-pkgs-$Platform-$ts.tar.gz.sha256" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/packages/offline/python/$Platform/$evidenceRunSegment/python-pkgs-$Platform-$ts.tar.gz.sha256"
  Upload-IfExists "$runDir\python-env-$Platform-$ts.tar.gz" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/python/$Platform/$evidenceRunSegment/python-env-$Platform-$ts.tar.gz"
  Upload-IfExists "$runDir\python-env-$Platform-$ts.tar.gz.sha256" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/python/$Platform/$evidenceRunSegment/python-env-$Platform-$ts.tar.gz.sha256"
  $metaJson = "{`"platform`":`"$Platform`",`"timestamp_utc`":`"$ts`",`"scan_execution_id`":`"$runId`",`"python_version`":`"$pythonVersion`",`"input_type`":`"$inputType`",`"materialize_after_scan`":`"$materializeAfterScan`",`"ephemeral_prefix`":`"${env:EPHEMERAL_PREFIX}/$Platform/$ts`",`"offline_bundle_prefix`":`"${env:EVIDENCE_PREFIX}/packages/offline/python/$Platform/$evidenceRunSegment`",`"cleanup`":`"requested`"}"
  $metaJson | Out-File "$runDir\run-metadata.json" -Encoding ascii
  aws s3 cp "$runDir\run-metadata.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/traceability/python/$Platform/$evidenceRunSegment/run-metadata.json" | Out-Null
  Write-State -Phase 'completed'
  aws s3 cp "$runDir\stage-state.json" "$checkpointPrefix/latest/stage-state.json" | Out-Null

  if ($govExit -ne 0) { throw "Governance gate failed with exit $govExit" }
  if ($materializationValidationExit -ne 0) { throw "Materialization validation failed with exit $materializationValidationExit" }
} catch {
  Stop-CheckpointLoop
  try { Write-State -Phase 'failed' } catch {}
  try { Upload-IfExists "$runDir\restore.log" "$checkpointPrefix/failures/restore.log" } catch {}
  try { Upload-IfExists "$runDir\stage-state.json" "$checkpointPrefix/failures/stage-state.json" } catch {}
  try { Publish-Checkpoint -Phase 'failed' } catch {}
  throw
}
