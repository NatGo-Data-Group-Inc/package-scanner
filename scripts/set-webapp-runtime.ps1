param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("enable", "disable", "reconcile")]
  [string]$Action,

  [Parameter(Mandatory = $false)]
  [int]$LeaseMinutes = 60,

  [Parameter(Mandatory = $false)]
  [int]$WaitTimeoutSeconds = 600,

  [Parameter(Mandatory = $false)]
  [string]$StackName = "package-scanner-webapp-dev",

  [Parameter(Mandatory = $false)]
  [string]$Region = "us-east-1",

  [Parameter(Mandatory = $false)]
  [string]$Profile = "",

  [Parameter(Mandatory = $false)]
  [switch]$AllowDefaultProfile
)

$ErrorActionPreference = "Stop"

if (-not $AllowDefaultProfile -and $Profile -eq "") {
  throw "Guardrail: -Profile is required unless -AllowDefaultProfile is explicitly set."
}

if (-not (Get-Command bash -ErrorAction SilentlyContinue)) {
  throw "bash is required in PATH to run this wrapper."
}

$scriptDir = Split-Path -Parent $PSCommandPath
$bashScript = Join-Path $scriptDir "set-webapp-runtime.sh"
if (-not (Test-Path $bashScript)) {
  throw "Bash script not found: $bashScript"
}

$bashArgs = @(
  "--action", $Action,
  "--lease-minutes", $LeaseMinutes.ToString(),
  "--wait-timeout-seconds", $WaitTimeoutSeconds.ToString(),
  "--stack-name", $StackName,
  "--region", $Region
)

if ($Profile -ne "") { $bashArgs += @("--profile", $Profile) }
if ($AllowDefaultProfile) { $bashArgs += "--allow-default-profile" }

& bash $bashScript @bashArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
