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
  [string]$StackName = "package-scanner-webapp-dev",

  [Parameter(Mandatory = $false)]
  [string]$EnvironmentName = "package-scanner-dev",

  [Parameter(Mandatory = $false)]
  [string]$PythonStackName = "cyber-scanner-dev-python-ecs",

  [Parameter(Mandatory = $false)]
  [string]$RStackName = "cyber-scanner-dev-r-ecs",

  [Parameter(Mandatory = $false)]
  [string]$CatalogBucketName = "",

  [Parameter(Mandatory = $false)]
  [string]$InputBucketName = "",

  [Parameter(Mandatory = $false)]
  [string]$EphemeralBucketName = "",

  [Parameter(Mandatory = $false)]
  [string]$CatalogPrefix = "evidence",

  [Parameter(Mandatory = $false)]
  [string]$EphemeralPrefix = "deploy/tmp/r",

  [Parameter(Mandatory = $false)]
  [string]$AllowedIngressCidr = "0.0.0.0/0",

  [Parameter(Mandatory = $false)]
  [string]$TlsCertificateArn = "",

  [Parameter(Mandatory = $false)]
  [string]$CustomDomainName = "",

  [Parameter(Mandatory = $false)]
  [string]$CustomDomainHostedZoneId = "",

  [Parameter(Mandatory = $false)]
  [ValidateSet("true", "false")]
  [string]$RuntimeEnabled = "false",

  [Parameter(Mandatory = $false)]
  [int]$IdleTimeoutMinutes = 60,

  [Parameter(Mandatory = $false)]
  [int]$DesiredCount = 0,

  [Parameter(Mandatory = $false)]
  [string]$TaskCpu = "1024",

  [Parameter(Mandatory = $false)]
  [string]$TaskMemory = "2048",

  [Parameter(Mandatory = $false)]
  [string]$ImageTag = "",

  [Parameter(Mandatory = $false)]
  [switch]$SkipImageBuild
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command bash -ErrorAction SilentlyContinue)) {
  throw "bash is required in PATH to run this wrapper."
}

$scriptDir = Split-Path -Parent $PSCommandPath
$bashScript = Join-Path $scriptDir "deploy-webapp-cfn.sh"
if (-not (Test-Path $bashScript)) {
  throw "Bash script not found: $bashScript"
}

$bashArgs = @(
  "--region", $Region,
  "--stack-name", $StackName,
  "--environment-name", $EnvironmentName,
  "--python-stack-name", $PythonStackName,
  "--r-stack-name", $RStackName,
  "--catalog-prefix", $CatalogPrefix,
  "--ephemeral-prefix", $EphemeralPrefix,
  "--allowed-ingress-cidr", $AllowedIngressCidr,
  "--tls-certificate-arn", $TlsCertificateArn,
  "--custom-domain-name", $CustomDomainName,
  "--custom-domain-hosted-zone-id", $CustomDomainHostedZoneId,
  "--runtime-enabled", $RuntimeEnabled,
  "--idle-timeout-minutes", $IdleTimeoutMinutes.ToString(),
  "--desired-count", $DesiredCount.ToString(),
  "--task-cpu", $TaskCpu,
  "--task-memory", $TaskMemory
)

if ($Profile -ne "") { $bashArgs += @("--profile", $Profile) }
if ($AllowDefaultProfile) { $bashArgs += "--allow-default-profile" }
if ($ExpectedAccountId -ne "") { $bashArgs += @("--expected-account-id", $ExpectedAccountId) }
if ($CatalogBucketName -ne "") { $bashArgs += @("--catalog-bucket-name", $CatalogBucketName) }
if ($InputBucketName -ne "") { $bashArgs += @("--input-bucket-name", $InputBucketName) }
if ($EphemeralBucketName -ne "") { $bashArgs += @("--ephemeral-bucket-name", $EphemeralBucketName) }
if ($ImageTag -ne "") { $bashArgs += @("--image-tag", $ImageTag) }
if ($SkipImageBuild) { $bashArgs += "--skip-image-build" }

& bash $bashScript @bashArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
