param(
  [Parameter(Mandatory = $true)]
  [string]$StackName,

  [Parameter(Mandatory = $true)]
  [string]$InputBucket,

  [Parameter(Mandatory = $false)]
  [string]$InputObjectKey = "inputs/python/environment.yml",

  [Parameter(Mandatory = $false)]
  [string]$EvidenceBucket = "",

  [Parameter(Mandatory = $false)]
  [string]$EvidencePrefix = "evidence",

  [Parameter(Mandatory = $false)]
  [string]$EphemeralBucket = "",

  [Parameter(Mandatory = $false)]
  [string]$EphemeralPrefix = "deploy/tmp/python",

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
  [switch]$EnableFortify,

  [Parameter(Mandatory = $false)]
  [string]$FortifyCommand = "",

  [Parameter(Mandatory = $false)]
  [bool]$RemediateMedium = $true,

  [Parameter(Mandatory = $false)]
  [bool]$FailOnMedium = $false,

  [Parameter(Mandatory = $false)]
  [string]$SafetyApiKey = ""
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command bash -ErrorAction SilentlyContinue)) {
  throw "bash is required in PATH to run this wrapper."
}

$scriptDir = Split-Path -Parent $PSCommandPath
$bashScript = Join-Path $scriptDir "start-python-scan.sh"
if (-not (Test-Path $bashScript)) {
  throw "Bash script not found: $bashScript"
}

$bashArgs = @(
  "--stack-name", $StackName,
  "--input-bucket", $InputBucket,
  "--input-object-key", $InputObjectKey,
  "--evidence-prefix", $EvidencePrefix,
  "--ephemeral-prefix", $EphemeralPrefix,
  "--region", $Region
)

if ($Profile -ne "") { $bashArgs += @("--profile", $Profile) }
if ($AllowDefaultProfile) { $bashArgs += "--allow-default-profile" }
if ($ExpectedAccountId -ne "") { $bashArgs += @("--expected-account-id", $ExpectedAccountId) }
if ($DeploymentLockToken -ne "") { $bashArgs += @("--deployment-lock-token", $DeploymentLockToken) }
if ($EvidenceBucket -ne "") { $bashArgs += @("--evidence-bucket", $EvidenceBucket) }
if ($EphemeralBucket -ne "") { $bashArgs += @("--ephemeral-bucket", $EphemeralBucket) }
if ($EnableFortify) { $bashArgs += "--enable-fortify" }
if ($FortifyCommand -ne "") { $bashArgs += @("--fortify-command", $FortifyCommand) }
$bashArgs += @("--remediate-medium", $RemediateMedium.ToString().ToLowerInvariant())
$bashArgs += @("--fail-on-medium", $FailOnMedium.ToString().ToLowerInvariant())
if ($SafetyApiKey -ne "") { $bashArgs += @("--safety-api-key", $SafetyApiKey) }

& bash $bashScript @bashArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
