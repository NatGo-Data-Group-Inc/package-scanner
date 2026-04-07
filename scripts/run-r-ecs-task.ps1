param(
  [Parameter(Mandatory = $true)]
  [string]$Platform
)

$ErrorActionPreference = 'Stop'
$ts = if ($env:SCAN_TIMESTAMP) { $env:SCAN_TIMESTAMP } else { (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') }
$baseDir = if ($env:SCAN_WORK_DIR) { $env:SCAN_WORK_DIR } else { 'C:\package-scanner-data' }
$runDir = Join-Path $baseDir "scan-out\$ts"
$projectDir = Join-Path $runDir 'project'
$cacheDir = Join-Path $runDir 'cache'
$libraryDir = Join-Path $runDir 'library'
$checkpointPrefix = "s3://${env:EPHEMERAL_BUCKET}/${env:EPHEMERAL_PREFIX}/checkpoints/r/$($env:SCAN_EXECUTION_ID)/$Platform"
$checkpointIntervalSeconds = if ($env:CHECKPOINT_INTERVAL_SECONDS) { [int]$env:CHECKPOINT_INTERVAL_SECONDS } else { 900 }
$scriptRoot = if ($env:SCRIPT_ROOT) { $env:SCRIPT_ROOT } else { 'C:\package-scanner\scripts' }
$checkpointJob = $null

function Write-State {
  param([string]$Phase)
  $payload = "{`"platform`":`"$Platform`",`"scan_execution_id`":`"$($env:SCAN_EXECUTION_ID)`",`"scan_timestamp`":`"$ts`",`"phase`":`"$Phase`",`"checkpoint_prefix`":`"$($checkpointPrefix.Replace('s3://', ''))`"}"
  $payload | Out-File "$runDir\stage-state.json" -Encoding ascii
}

function Upload-IfExists {
  param([string]$Path, [string]$Destination)
  if (Test-Path $Path) {
    aws s3 cp $Path $Destination | Out-Null
  }
}

function Publish-Checkpoint {
  param([string]$Phase = 'restore')
  Write-State -Phase $Phase
  if (Test-Path $cacheDir) {
    python "$scriptRoot\bundle-directory.py" --source-dir $cacheDir --output-file "$runDir\checkpoint-renv-cache.tar.gz" --checksum-file "$runDir\checkpoint-renv-cache.tar.gz.sha256"
    aws s3 cp "$runDir\checkpoint-renv-cache.tar.gz" "$checkpointPrefix/latest/renv-cache.tar.gz" | Out-Null
    aws s3 cp "$runDir\checkpoint-renv-cache.tar.gz.sha256" "$checkpointPrefix/latest/renv-cache.tar.gz.sha256" | Out-Null
  }
  if (Test-Path $libraryDir) {
    python "$scriptRoot\bundle-directory.py" --source-dir $libraryDir --output-file "$runDir\checkpoint-renv-library.tar.gz" --checksum-file "$runDir\checkpoint-renv-library.tar.gz.sha256"
    aws s3 cp "$runDir\checkpoint-renv-library.tar.gz" "$checkpointPrefix/latest/renv-library.tar.gz" | Out-Null
    aws s3 cp "$runDir\checkpoint-renv-library.tar.gz.sha256" "$checkpointPrefix/latest/renv-library.tar.gz.sha256" | Out-Null
  }
  aws s3 cp "$runDir\stage-state.json" "$checkpointPrefix/latest/stage-state.json" | Out-Null
}

function Stop-CheckpointLoop {
  if ($checkpointJob) {
    Stop-Job $checkpointJob -Force -ErrorAction SilentlyContinue | Out-Null
    Remove-Job $checkpointJob -Force -ErrorAction SilentlyContinue | Out-Null
    $script:checkpointJob = $null
  }
}

function Start-CheckpointLoop {
  $script:checkpointJob = Start-Job -ScriptBlock {
    param($CheckpointIntervalSeconds, $runDir, $cacheDir, $libraryDir, $checkpointPrefix, $Platform, $ScanExecutionId, $ts, $scriptRoot)
    function Write-StateInner {
      param([string]$Phase)
      $payload = "{`"platform`":`"$Platform`",`"scan_execution_id`":`"$ScanExecutionId`",`"scan_timestamp`":`"$ts`",`"phase`":`"$Phase`",`"checkpoint_prefix`":`"$($checkpointPrefix.Replace('s3://', ''))`"}"
      $payload | Out-File "$runDir\stage-state.json" -Encoding ascii
    }
    while ($true) {
      Start-Sleep -Seconds $CheckpointIntervalSeconds
      try {
        Write-StateInner -Phase 'restore'
        if (Test-Path $cacheDir) {
          python "$scriptRoot\bundle-directory.py" --source-dir $cacheDir --output-file "$runDir\checkpoint-renv-cache.tar.gz" --checksum-file "$runDir\checkpoint-renv-cache.tar.gz.sha256"
          aws s3 cp "$runDir\checkpoint-renv-cache.tar.gz" "$checkpointPrefix/latest/renv-cache.tar.gz" | Out-Null
          aws s3 cp "$runDir\checkpoint-renv-cache.tar.gz.sha256" "$checkpointPrefix/latest/renv-cache.tar.gz.sha256" | Out-Null
        }
        if (Test-Path $libraryDir) {
          python "$scriptRoot\bundle-directory.py" --source-dir $libraryDir --output-file "$runDir\checkpoint-renv-library.tar.gz" --checksum-file "$runDir\checkpoint-renv-library.tar.gz.sha256"
          aws s3 cp "$runDir\checkpoint-renv-library.tar.gz" "$checkpointPrefix/latest/renv-library.tar.gz" | Out-Null
          aws s3 cp "$runDir\checkpoint-renv-library.tar.gz.sha256" "$checkpointPrefix/latest/renv-library.tar.gz.sha256" | Out-Null
        }
        aws s3 cp "$runDir\stage-state.json" "$checkpointPrefix/latest/stage-state.json" | Out-Null
      } catch {}
    }
  } -ArgumentList $checkpointIntervalSeconds, $runDir, $cacheDir, $libraryDir, $checkpointPrefix, $Platform, $env:SCAN_EXECUTION_ID, $ts, $scriptRoot
}

function Publish-FailureDiagnostics {
  Stop-CheckpointLoop
  Write-State -Phase 'failed'
  Upload-IfExists -Path "$runDir\preflight-native-deps.txt" -Destination "$checkpointPrefix/failures/preflight-native-deps.txt"
  Upload-IfExists -Path "$runDir\preflight-native-deps.json" -Destination "$checkpointPrefix/failures/preflight-native-deps.json"
  Upload-IfExists -Path "$runDir\restore.log" -Destination "$checkpointPrefix/failures/restore.log"
  Upload-IfExists -Path "$runDir\stage-state.json" -Destination "$checkpointPrefix/failures/stage-state.json"
  try { Publish-Checkpoint -Phase 'failed' } catch {}
}

trap {
  Publish-FailureDiagnostics
  throw
}

New-Item -ItemType Directory -Force -Path $runDir, $projectDir, $cacheDir, $libraryDir, 'C:\scan-input' | Out-Null
aws s3 cp "s3://${env:INPUT_BUCKET}/${env:INPUT_OBJECT_KEY}" C:\scan-input\renv.lock | Out-Null
Copy-Item C:\scan-input\renv.lock "$runDir\renv.lock" -Force

try {
  aws s3 cp "$checkpointPrefix/latest/renv-cache.tar.gz" "$runDir\checkpoint-renv-cache.tar.gz" | Out-Null
  python "$scriptRoot\extract-archive.py" --archive "$runDir\checkpoint-renv-cache.tar.gz" --destination $cacheDir
} catch {}
try {
  aws s3 cp "$checkpointPrefix/latest/renv-library.tar.gz" "$runDir\checkpoint-renv-library.tar.gz" | Out-Null
  python "$scriptRoot\extract-archive.py" --archive "$runDir\checkpoint-renv-library.tar.gz" --destination $libraryDir
} catch {}

$rVersion = ((Get-Content "$runDir\renv.lock" -Raw | ConvertFrom-Json).R.Version)
$actualVersion = (& Rscript -e "cat(as.character(getRversion()))")
if ($actualVersion -ne $rVersion) {
  throw "R version mismatch. image=$actualVersion lockfile=$rVersion"
}

Write-State -Phase 'preflight'
python "$scriptRoot\preflight-r-native-deps.py" --lock-file "$runDir\renv.lock" --platform $Platform --output-json "$runDir\preflight-native-deps.json" --output-text "$runDir\preflight-native-deps.txt"
Upload-IfExists -Path "$runDir\preflight-native-deps.json" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/traceability/r/$Platform/$ts/preflight-native-deps.json"
Upload-IfExists -Path "$runDir\preflight-native-deps.txt" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/preflight-native-deps.txt"

Write-State -Phase 'restore'
Start-CheckpointLoop

Rscript "$scriptRoot\materialize-r-environment.R" --lock-file "$runDir\renv.lock" --project-dir $projectDir --cache-dir $cacheDir --library-dir $libraryDir --output-dir $runDir --platform $Platform --clean false

Stop-CheckpointLoop
Publish-Checkpoint -Phase 'restored'

$libraryPath = (Get-Content "$runDir\library-path.txt" -Raw).Trim()
python "$scriptRoot\bundle-directory.py" --source-dir $cacheDir --output-file "$runDir\renv-cache-$Platform-$ts.tar.gz" --checksum-file "$runDir\renv-cache-$Platform-$ts.tar.gz.sha256"
python "$scriptRoot\bundle-directory.py" --source-dir $libraryPath --output-file "$runDir\renv-library-$Platform-$ts.tar.gz" --checksum-file "$runDir\renv-library-$Platform-$ts.tar.gz.sha256"
python "$scriptRoot\generate-r-materialization-summary.py" --run-dir $runDir --platform $Platform --r-version $rVersion --cache-dir $cacheDir --library-path $libraryPath
python "$scriptRoot\generate-r-sbom.py" --installed-packages-file "$runDir\installed-packages.csv" --out-file "$runDir\r-packages.cdx.json"
python "$scriptRoot\scan-r-vulnerabilities.py" --installed-packages-file "$runDir\installed-packages.csv" --lock-file "$runDir\renv.lock" --out-file "$runDir\osv-report.json"
trivy sbom --format json --output "$runDir\trivy-sbom-report.json" "$runDir\r-packages.cdx.json"; $true
$govExit = 0
python "$scriptRoot\generate-r-governance-artifacts.py" --run-dir $runDir --platform $Platform --remediate-medium $env:REMEDIATE_MEDIUM --fail-on-medium $env:FAIL_ON_MEDIUM --remediate-unknown $env:REMEDIATE_UNKNOWN --fail-on-unknown $env:FAIL_ON_UNKNOWN
if ($LASTEXITCODE -ne 0) { $govExit = $LASTEXITCODE }
try { tar -czf "$runDir\environment-artifacts.tar.gz" -C $runDir renv.lock installed-packages.csv session-info.txt renv-status.txt materialization-summary.json r-packages.cdx.json } catch {}
aws s3 cp "$runDir\" "s3://${env:EPHEMERAL_BUCKET}/${env:EPHEMERAL_PREFIX}/$Platform/$ts/" --recursive | Out-Null
aws s3 cp "$runDir\renv.lock" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/requirements/r/$Platform/$ts/renv.lock" | Out-Null
Upload-IfExists -Path "$runDir\approval-candidate-packages.csv" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/requirements/r/$Platform/$ts/approval-candidate-packages.csv"
Upload-IfExists -Path "$runDir\installed-packages.csv" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/requirements/r/$Platform/$ts/installed-packages.csv"
Upload-IfExists -Path "$runDir\r-packages.cdx.json" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/r-packages.cdx.json"
Upload-IfExists -Path "$runDir\session-info.txt" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/session-info.txt"
Upload-IfExists -Path "$runDir\renv-status.txt" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/renv-status.txt"
Upload-IfExists -Path "$runDir\preflight-native-deps.txt" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/preflight-native-deps.txt"
Upload-IfExists -Path "$runDir\restore.log" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/restore.log"
Upload-IfExists -Path "$runDir\materialization-summary.json" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/traceability/r/$Platform/$ts/materialization-summary.json"
Upload-IfExists -Path "$runDir\preflight-native-deps.json" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/traceability/r/$Platform/$ts/preflight-native-deps.json"
Upload-IfExists -Path "$runDir\environment-artifacts.tar.gz" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/environment-artifacts.tar.gz"
Upload-IfExists -Path "$runDir\renv-library-$Platform-$ts.tar.gz" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/renv-library-$Platform-$ts.tar.gz"
Upload-IfExists -Path "$runDir\renv-library-$Platform-$ts.tar.gz.sha256" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/renv-library-$Platform-$ts.tar.gz.sha256"
Upload-IfExists -Path "$runDir\renv-cache-$Platform-$ts.tar.gz" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/packages/offline/r/$Platform/$ts/renv-cache-$Platform-$ts.tar.gz"
Upload-IfExists -Path "$runDir\renv-cache-$Platform-$ts.tar.gz.sha256" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/packages/offline/r/$Platform/$ts/renv-cache-$Platform-$ts.tar.gz.sha256"
Upload-IfExists -Path "$runDir\trivy-sbom-report.json" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/model-results/r/$Platform/$ts/trivy-sbom-report.json"
Upload-IfExists -Path "$runDir\osv-report.json" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/model-results/r/$Platform/$ts/osv-report.json"
Upload-IfExists -Path "$runDir\vulnerability-findings.csv" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/governance/r/$Platform/$ts/vulnerability-findings.csv"
Upload-IfExists -Path "$runDir\remediation-required.csv" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/governance/r/$Platform/$ts/remediation-required.csv"
Upload-IfExists -Path "$runDir\remediation-exceptions.csv" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/governance/r/$Platform/$ts/remediation-exceptions.csv"
Upload-IfExists -Path "$runDir\remediation-spreadsheet.csv" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/governance/r/$Platform/$ts/remediation-spreadsheet.csv"
Upload-IfExists -Path "$runDir\governance-summary.json" -Destination "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/traceability/r/$Platform/$ts/governance-summary.json"
$metaJson = "{`"platform`":`"$Platform`",`"timestamp_utc`":`"$ts`",`"scan_execution_id`":`"$($env:SCAN_EXECUTION_ID)`",`"r_version`":`"$rVersion`",`"ephemeral_prefix`":`"${env:EPHEMERAL_PREFIX}/$Platform/$ts`",`"offline_bundle_prefix`":`"${env:EVIDENCE_PREFIX}/packages/offline/r/$Platform/$ts`",`"cleanup`":`"requested`"}"
$metaJson | Out-File "$runDir\run-metadata.json" -Encoding ascii
aws s3 cp "$runDir\run-metadata.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/traceability/r/$Platform/$ts/run-metadata.json" | Out-Null
Write-State -Phase 'completed'
aws s3 cp "$runDir\stage-state.json" "$checkpointPrefix/latest/stage-state.json" | Out-Null
if ($govExit -ne 0) { throw "Governance gate failed with exit $govExit" }
