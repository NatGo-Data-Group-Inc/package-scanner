param(
  [Parameter(Mandatory = $true)]
  [string]$Platform
)

$ErrorActionPreference = 'Stop'
$ts = if ($env:SCAN_TIMESTAMP) { $env:SCAN_TIMESTAMP } else { (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') }
$runDir = "C:\scan-out\$ts"
$projectDir = "$runDir\project"
$cacheDir = "$runDir\cache"
$libraryDir = "$runDir\library"
$stageIndex = if ($env:STAGE_INDEX) { $env:STAGE_INDEX } else { '1' }
$totalStages = if ($env:TOTAL_STAGES) { $env:TOTAL_STAGES } else { '1' }
$finalStage = if ($env:FINAL_STAGE) { $env:FINAL_STAGE } else { 'true' }
$checkpointPrefix = "s3://${env:EPHEMERAL_BUCKET}/${env:EPHEMERAL_PREFIX}/checkpoints/r/$($env:SCAN_EXECUTION_ID)/$Platform"

New-Item -ItemType Directory -Force -Path $runDir, $projectDir, $cacheDir, $libraryDir | Out-Null
aws s3 cp "s3://${env:INPUT_BUCKET}/${env:INPUT_OBJECT_KEY}" C:\scan-input\renv.lock
Copy-Item C:\scan-input\renv.lock "$runDir\renv.lock" -Force
$rVersion = ((Get-Content C:\scan-input\renv.lock -Raw | ConvertFrom-Json).R.Version)
powershell -ExecutionPolicy Bypass -File C:\install-r-runtime.ps1 -RVersion $rVersion
$env:PATH = "C:\R\R-$rVersion\bin;C:\rtools44\usr\bin;C:\rtools44\mingw64\bin;$env:PATH"

if ($stageIndex -ne '1') {
  aws s3 cp "$checkpointPrefix/latest/renv-cache.tar.gz" "$runDir\checkpoint-renv-cache.tar.gz"
  aws s3 cp "$checkpointPrefix/latest/renv-library.tar.gz" "$runDir\checkpoint-renv-library.tar.gz"
  python C:\extract-archive.py --archive "$runDir\checkpoint-renv-cache.tar.gz" --destination $cacheDir
  python C:\extract-archive.py --archive "$runDir\checkpoint-renv-library.tar.gz" --destination $libraryDir
}

$packagesJson = if ($env:STAGE_PACKAGES_JSON) { $env:STAGE_PACKAGES_JSON } else { '[]' }
$packagesJson | Out-File "$runDir\stage-packages.json" -Encoding ascii
$packages = $packagesJson | ConvertFrom-Json
$packageLines = @()
foreach ($package in $packages) { $packageLines += [string]$package }
$packageLines | Out-File "$runDir\stage-packages.txt" -Encoding ascii

& "C:\R\R-$rVersion\bin\Rscript.exe" C:\materialize-r-environment.R --lock-file "$runDir\renv.lock" --project-dir $projectDir --cache-dir $cacheDir --library-dir $libraryDir --output-dir $runDir --platform $Platform --packages-file "$runDir\stage-packages.txt" --clean false

python C:\bundle-directory.py --source-dir $cacheDir --output-file "$runDir\checkpoint-renv-cache.tar.gz" --checksum-file "$runDir\checkpoint-renv-cache.tar.gz.sha256"
python C:\bundle-directory.py --source-dir $libraryDir --output-file "$runDir\checkpoint-renv-library.tar.gz" --checksum-file "$runDir\checkpoint-renv-library.tar.gz.sha256"
aws s3 cp "$runDir\checkpoint-renv-cache.tar.gz" "$checkpointPrefix/latest/renv-cache.tar.gz"
aws s3 cp "$runDir\checkpoint-renv-library.tar.gz" "$checkpointPrefix/latest/renv-library.tar.gz"

$stageState = "{`"platform`":`"$Platform`",`"scan_execution_id`":`"$($env:SCAN_EXECUTION_ID)`",`"scan_timestamp`":`"$ts`",`"stage_index`":$stageIndex,`"total_stages`":$totalStages,`"final_stage`":`"$finalStage`",`"checkpoint_prefix`":`"$($checkpointPrefix.Replace('s3://', ''))`"}"
$stageState | Out-File "$runDir\stage-state.json" -Encoding ascii
aws s3 cp "$runDir\stage-state.json" "$checkpointPrefix/latest/stage-state.json"

if ($finalStage -ne 'true') {
  aws s3 cp "$runDir\restore.log" "$checkpointPrefix/stages/$stageIndex/restore.log"
  aws s3 cp "$runDir\stage-packages.json" "$checkpointPrefix/stages/$stageIndex/packages.json"
  aws s3 cp "$runDir\stage-state.json" "$checkpointPrefix/stages/$stageIndex/stage-state.json"
  exit 0
}

$libraryPath = (Get-Content "$runDir\library-path.txt" -Raw).Trim()
python C:\bundle-directory.py --source-dir $cacheDir --output-file "$runDir\renv-cache-$Platform-$ts.tar.gz" --checksum-file "$runDir\renv-cache-$Platform-$ts.tar.gz.sha256"
python C:\bundle-directory.py --source-dir $libraryPath --output-file "$runDir\renv-library-$Platform-$ts.tar.gz" --checksum-file "$runDir\renv-library-$Platform-$ts.tar.gz.sha256"
python C:\generate-r-materialization-summary.py --run-dir $runDir --platform $Platform --r-version $rVersion --cache-dir $cacheDir --library-path $libraryPath
python C:\generate-r-sbom.py --installed-packages-file "$runDir\installed-packages.csv" --out-file "$runDir\r-packages.cdx.json"
python C:\scan-r-vulnerabilities.py --installed-packages-file "$runDir\installed-packages.csv" --lock-file "$runDir\renv.lock" --out-file "$runDir\osv-report.json"
C:\trivy\trivy.exe sbom --format json --output "$runDir\trivy-sbom-report.json" "$runDir\r-packages.cdx.json"; $true
$govExit = 0
python C:\generate-r-governance-artifacts.py --run-dir $runDir --platform $Platform --remediate-medium $env:REMEDIATE_MEDIUM --fail-on-medium $env:FAIL_ON_MEDIUM --remediate-unknown $env:REMEDIATE_UNKNOWN --fail-on-unknown $env:FAIL_ON_UNKNOWN
if ($LASTEXITCODE -ne 0) { $govExit = $LASTEXITCODE }
try { tar -czf "$runDir\environment-artifacts.tar.gz" -C $runDir renv.lock installed-packages.csv session-info.txt renv-status.txt materialization-summary.json r-packages.cdx.json } catch { Write-Host 'archive step skipped' }
aws s3 cp "$runDir\" "s3://${env:EPHEMERAL_BUCKET}/${env:EPHEMERAL_PREFIX}/$Platform/$ts/" --recursive
aws s3 cp "$runDir\renv.lock" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/requirements/r/$Platform/$ts/renv.lock"
if (Test-Path "$runDir\approval-candidate-packages.csv") { aws s3 cp "$runDir\approval-candidate-packages.csv" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/requirements/r/$Platform/$ts/approval-candidate-packages.csv" }
if (Test-Path "$runDir\installed-packages.csv") { aws s3 cp "$runDir\installed-packages.csv" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/requirements/r/$Platform/$ts/installed-packages.csv" }
if (Test-Path "$runDir\r-packages.cdx.json") { aws s3 cp "$runDir\r-packages.cdx.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/r-packages.cdx.json" }
if (Test-Path "$runDir\session-info.txt") { aws s3 cp "$runDir\session-info.txt" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/session-info.txt" }
if (Test-Path "$runDir\renv-status.txt") { aws s3 cp "$runDir\renv-status.txt" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/renv-status.txt" }
if (Test-Path "$runDir\restore.log") { aws s3 cp "$runDir\restore.log" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/restore.log" }
if (Test-Path "$runDir\materialization-summary.json") { aws s3 cp "$runDir\materialization-summary.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/traceability/r/$Platform/$ts/materialization-summary.json" }
if (Test-Path "$runDir\environment-artifacts.tar.gz") { aws s3 cp "$runDir\environment-artifacts.tar.gz" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/environment-artifacts.tar.gz" }
if (Test-Path "$runDir\renv-library-$Platform-$ts.tar.gz") { aws s3 cp "$runDir\renv-library-$Platform-$ts.tar.gz" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/renv-library-$Platform-$ts.tar.gz" }
if (Test-Path "$runDir\renv-library-$Platform-$ts.tar.gz.sha256") { aws s3 cp "$runDir\renv-library-$Platform-$ts.tar.gz.sha256" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/env-artifacts/r/$Platform/$ts/renv-library-$Platform-$ts.tar.gz.sha256" }
if (Test-Path "$runDir\renv-cache-$Platform-$ts.tar.gz") { aws s3 cp "$runDir\renv-cache-$Platform-$ts.tar.gz" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/packages/offline/r/$Platform/$ts/renv-cache-$Platform-$ts.tar.gz" }
if (Test-Path "$runDir\renv-cache-$Platform-$ts.tar.gz.sha256") { aws s3 cp "$runDir\renv-cache-$Platform-$ts.tar.gz.sha256" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/packages/offline/r/$Platform/$ts/renv-cache-$Platform-$ts.tar.gz.sha256" }
if (Test-Path "$runDir\trivy-sbom-report.json") { aws s3 cp "$runDir\trivy-sbom-report.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/model-results/r/$Platform/$ts/trivy-sbom-report.json" }
if (Test-Path "$runDir\osv-report.json") { aws s3 cp "$runDir\osv-report.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/model-results/r/$Platform/$ts/osv-report.json" }
if (Test-Path "$runDir\vulnerability-findings.csv") { aws s3 cp "$runDir\vulnerability-findings.csv" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/governance/r/$Platform/$ts/vulnerability-findings.csv" }
if (Test-Path "$runDir\remediation-required.csv") { aws s3 cp "$runDir\remediation-required.csv" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/governance/r/$Platform/$ts/remediation-required.csv" }
if (Test-Path "$runDir\remediation-exceptions.csv") { aws s3 cp "$runDir\remediation-exceptions.csv" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/governance/r/$Platform/$ts/remediation-exceptions.csv" }
if (Test-Path "$runDir\remediation-spreadsheet.csv") { aws s3 cp "$runDir\remediation-spreadsheet.csv" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/governance/r/$Platform/$ts/remediation-spreadsheet.csv" }
if (Test-Path "$runDir\governance-summary.json") { aws s3 cp "$runDir\governance-summary.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/traceability/r/$Platform/$ts/governance-summary.json" }
$metaJson = "{`"platform`":`"$Platform`",`"timestamp_utc`":`"$ts`",`"scan_execution_id`":`"$($env:SCAN_EXECUTION_ID)`",`"r_version`":`"$rVersion`",`"ephemeral_prefix`":`"${env:EPHEMERAL_PREFIX}/$Platform/$ts`",`"offline_bundle_prefix`":`"${env:EVIDENCE_PREFIX}/packages/offline/r/$Platform/$ts`",`"cleanup`":`"requested`",`"stage_index`":`"$stageIndex`",`"total_stages`":`"$totalStages`"}"
$metaJson | Out-File "$runDir\run-metadata.json" -Encoding ascii
aws s3 cp "$runDir\run-metadata.json" "s3://${env:EVIDENCE_BUCKET}/${env:EVIDENCE_PREFIX}/traceability/r/$Platform/$ts/run-metadata.json"
if ($govExit -ne 0) { Write-Host "Governance gate failed with exit $govExit"; exit $govExit }
