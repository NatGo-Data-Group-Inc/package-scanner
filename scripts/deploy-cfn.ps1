param(
  [Parameter(Mandatory = $false)]
  [string]$Region = "us-east-1",

  [Parameter(Mandatory = $false)]
  [string]$Profile = "",

  [Parameter(Mandatory = $false)]
  [switch]$AllowDefaultProfile,

  [Parameter(Mandatory = $false)]
  [string]$ExpectedAccountId = "",

  [Parameter(Mandatory = $false)]
  [string]$StackName = "cyber-scanner-dev",

  [Parameter(Mandatory = $false)]
  [string]$EnvironmentName = "cyber-scanner-dev",

  [Parameter(Mandatory = $true)]
  [string]$DeploymentLockToken,

  [Parameter(Mandatory = $false)]
  [string]$ExistingInputBucketName = "",

  [Parameter(Mandatory = $false)]
  [string]$ExistingEvidenceBucketName = "",

  [Parameter(Mandatory = $false)]
  [string]$ExistingEphemeralBucketName = "",

  [Parameter(Mandatory = $false)]
  [int]$BuildTimeoutMinutes = 90,

  [Parameter(Mandatory = $false)]
  [string]$LinuxComputeType = "BUILD_GENERAL1_MEDIUM",

  [Parameter(Mandatory = $false)]
  [string]$LinuxArmComputeType = "BUILD_GENERAL1_LARGE",

  [Parameter(Mandatory = $false)]
  [string]$WindowsComputeType = "BUILD_GENERAL1_LARGE",

  [Parameter(Mandatory = $false)]
  [string]$TrivyVersion = "0.69.3",

  [Parameter(Mandatory = $false)]
  [string]$TrivyReleaseBaseUrl = "https://github.com/aquasecurity/trivy/releases/download"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command bash -ErrorAction SilentlyContinue)) {
  throw "bash is required in PATH to run this wrapper."
}

$scriptDir = Split-Path -Parent $PSCommandPath
$bashScript = Join-Path $scriptDir "deploy-cfn.sh"
if (-not (Test-Path $bashScript)) {
  throw "Bash script not found: $bashScript"
}

$bashArgs = @(
  "--region", $Region,
  "--stack-name", $StackName,
  "--environment-name", $EnvironmentName,
  "--build-timeout-minutes", "$BuildTimeoutMinutes",
  "--linux-compute-type", $LinuxComputeType,
  "--linux-arm-compute-type", $LinuxArmComputeType,
  "--windows-compute-type", $WindowsComputeType
  "--trivy-version", $TrivyVersion,
  "--trivy-release-base-url", $TrivyReleaseBaseUrl
)

if ($Profile -ne "") { $bashArgs += @("--profile", $Profile) }
if ($AllowDefaultProfile) { $bashArgs += "--allow-default-profile" }
if ($ExpectedAccountId -ne "") { $bashArgs += @("--expected-account-id", $ExpectedAccountId) }
if ($DeploymentLockToken -ne "") { $bashArgs += @("--deployment-lock-token", $DeploymentLockToken) }
if ($ExistingInputBucketName -ne "") { $bashArgs += @("--existing-input-bucket-name", $ExistingInputBucketName) }
if ($ExistingEvidenceBucketName -ne "") { $bashArgs += @("--existing-evidence-bucket-name", $ExistingEvidenceBucketName) }
if ($ExistingEphemeralBucketName -ne "") { $bashArgs += @("--existing-ephemeral-bucket-name", $ExistingEphemeralBucketName) }

& bash $bashScript @bashArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
