param(
  [Parameter(Mandatory = $true)]
  [string]$StackName,

  [Parameter(Mandatory = $true)]
  [string]$InputBucket,

  [Parameter(Mandatory = $false)]
  [string]$InputObjectKey = "inputs/r/renv.lock",

  [Parameter(Mandatory = $false)]
  [string]$SourceLockFile = "",

  [Parameter(Mandatory = $false)]
  [string]$SourceRequestedFile = "",

  [Parameter(Mandatory = $false)]
  [string]$EvidenceBucket = "",

  [Parameter(Mandatory = $false)]
  [string]$EvidencePrefix = "evidence",

  [Parameter(Mandatory = $false)]
  [string]$EphemeralBucket = "",

  [Parameter(Mandatory = $false)]
  [string]$EphemeralPrefix = "deploy/tmp/r",

  [Parameter(Mandatory = $false)]
  [string]$Region = "us-east-1",

  [Parameter(Mandatory = $false)]
  [string]$Profile = "",

  [Parameter(Mandatory = $false)]
  [switch]$AllowDefaultProfile,

  [Parameter(Mandatory = $false)]
  [string]$ExpectedAccountId = "",

  [Parameter(Mandatory = $true)]
  [string]$DeploymentLockToken,

  [Parameter(Mandatory = $false)]
  [bool]$RemediateMedium = $true,

  [Parameter(Mandatory = $false)]
  [bool]$FailOnMedium = $false,

  [Parameter(Mandatory = $false)]
  [bool]$RemediateUnknown = $true,

  [Parameter(Mandatory = $false)]
  [bool]$FailOnUnknown = $false,

  [Parameter(Mandatory = $false)]
  [int]$RStagePackageCount = 25,
  [Parameter(Mandatory = $false)]
  [ValidateSet("all", "linux-only", "windows-only")]
  [string]$PlatformSet = "all"
)

$ErrorActionPreference = "Stop"

$bashExe = "bash"
$gitBash = "C:\Program Files\Git\bin\bash.exe"
if (Test-Path $gitBash) {
  $bashExe = $gitBash
} elseif (-not (Get-Command bash -ErrorAction SilentlyContinue)) {
  throw "bash is required in PATH to run this wrapper."
}

$scriptDir = Split-Path -Parent $PSCommandPath
$bashScript = Join-Path $scriptDir "start-r-scan.sh"
if (-not (Test-Path $bashScript)) {
  throw "Bash script not found: $bashScript"
}
$bashScriptForBash = $bashScript -replace "\\", "/"
if ($bashScriptForBash -match "^([A-Za-z]):/(.*)$") {
  $bashScriptForBash = "/" + $Matches[1].ToLowerInvariant() + "/" + $Matches[2]
}

$bashArgs = @(
  "--stack-name", $StackName,
  "--input-bucket", $InputBucket,
  "--input-object-key", $InputObjectKey,
  "--evidence-prefix", $EvidencePrefix,
  "--ephemeral-prefix", $EphemeralPrefix,
  "--region", $Region
)

if ($SourceLockFile -ne "") { $bashArgs += @("--source-lock-file", $SourceLockFile) }
if ($SourceRequestedFile -ne "") { $bashArgs += @("--source-requested-file", $SourceRequestedFile) }
if ($Profile -ne "") { $bashArgs += @("--profile", $Profile) }
if ($AllowDefaultProfile) { $bashArgs += "--allow-default-profile" }
if ($ExpectedAccountId -ne "") { $bashArgs += @("--expected-account-id", $ExpectedAccountId) }
if ($DeploymentLockToken -ne "") { $bashArgs += @("--deployment-lock-token", $DeploymentLockToken) }
if ($EvidenceBucket -ne "") { $bashArgs += @("--evidence-bucket", $EvidenceBucket) }
if ($EphemeralBucket -ne "") { $bashArgs += @("--ephemeral-bucket", $EphemeralBucket) }
$bashArgs += @("--remediate-medium", $RemediateMedium.ToString().ToLowerInvariant())
$bashArgs += @("--fail-on-medium", $FailOnMedium.ToString().ToLowerInvariant())
$bashArgs += @("--remediate-unknown", $RemediateUnknown.ToString().ToLowerInvariant())
$bashArgs += @("--fail-on-unknown", $FailOnUnknown.ToString().ToLowerInvariant())
$bashArgs += @("--r-stage-package-count", $RStagePackageCount.ToString())
$bashArgs += @("--platform-set", $PlatformSet)

& $bashExe $bashScriptForBash @bashArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
