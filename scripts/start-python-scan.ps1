param(
  [Parameter(Mandatory = $true)]
  [string]$StackName,

  [Parameter(Mandatory = $true)]
  [string]$InputBucket,

  [Parameter(Mandatory = $false)]
  [string]$InputObjectKey = "inputs/python/environment.yml",

  [Parameter(Mandatory = $false)]
  [string]$SourceEnvironmentFile = "",

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
  [string]$SafetyApiKey = "",

  [Parameter(Mandatory = $false)]
  [ValidateSet("all", "linux-only", "linux-amd64", "linux-arm64", "windows-only")]
  [string]$PlatformSet = "all"
)

$ErrorActionPreference = "Stop"

function Get-AwsArgs {
  $args = @("--region", $Region)
  if ($Profile -ne "") { $args += @("--profile", $Profile) }
  return $args
}

function Get-StackOutput {
  param([string]$OutputKey)
  $value = (& aws cloudformation describe-stacks `
    --stack-name $StackName `
    --query "Stacks[0].Outputs[?OutputKey=='$OutputKey'].OutputValue | [0]" `
    --output text `
    @(Get-AwsArgs)).Trim()
  if ($LASTEXITCODE -ne 0) { throw "Failed to resolve stack output $OutputKey" }
  return $value
}

function Get-ClusterNameFromArn {
  param([string]$ClusterArn)
  return ($ClusterArn -split "/")[-1]
}

function Ensure-WorkerCapacity {
  param(
    [string]$ClusterArn,
    [string]$AsgOutputKey
  )

  $clusterName = Get-ClusterNameFromArn $ClusterArn
  $asgName = Get-StackOutput $AsgOutputKey
  if ([string]::IsNullOrWhiteSpace($asgName) -or $asgName -eq "None") {
    $asgName = $clusterName
  }

  $asgJson = & aws autoscaling describe-auto-scaling-groups `
    --auto-scaling-group-names $asgName `
    --query "AutoScalingGroups[0].{MinSize:MinSize,DesiredCapacity:DesiredCapacity}" `
    --output json `
    @(Get-AwsArgs)
  if ($LASTEXITCODE -ne 0) { throw "Failed to describe Auto Scaling group $asgName" }
  $asg = $asgJson | ConvertFrom-Json
  if ($null -eq $asg) { throw "Auto Scaling group not found: $asgName" }

  if (($asg.MinSize -lt 1) -or ($asg.DesiredCapacity -lt 1)) {
    Write-Host "Scaling $asgName for $clusterName to minimum worker capacity (min=1 desired=1)"
    & aws autoscaling update-auto-scaling-group `
      --auto-scaling-group-name $asgName `
      --min-size 1 `
      --desired-capacity 1 `
      @(Get-AwsArgs) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to update Auto Scaling group $asgName" }
  }

  Write-Host "Waiting for an ACTIVE ECS container instance in $clusterName"
  for ($attempt = 0; $attempt -lt 40; $attempt++) {
    $activeCount = (& aws ecs list-container-instances `
      --cluster $clusterName `
      --status ACTIVE `
      --query "length(containerInstanceArns)" `
      --output text `
      @(Get-AwsArgs)).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Failed to query ECS container instances for $clusterName" }
    if ($activeCount -match '^\d+$' -and [int]$activeCount -gt 0) {
      Write-Host "Cluster $clusterName has $activeCount active container instance(s)"
      return
    }
    Start-Sleep -Seconds 15
  }

  throw "Timed out waiting for ACTIVE ECS capacity in cluster $clusterName"
}

$bashExe = "bash"
$gitBash = "C:\Program Files\Git\bin\bash.exe"
if (Test-Path $gitBash) {
  $bashExe = $gitBash
} elseif (-not (Get-Command bash -ErrorAction SilentlyContinue)) {
  throw "bash is required in PATH to run this wrapper."
}

$scriptDir = Split-Path -Parent $PSCommandPath
$bashScript = Join-Path $scriptDir "start-python-scan.sh"
if (-not (Test-Path $bashScript)) {
  throw "Bash script not found: $bashScript"
}
$bashScriptForBash = $bashScript -replace "\\", "/"
if ($bashScriptForBash -match "^([A-Za-z]):/(.*)$") {
  $bashScriptForBash = "/" + $Matches[1].ToLowerInvariant() + "/" + $Matches[2]
}

switch ($PlatformSet) {
  "linux-amd64" {
    Ensure-WorkerCapacity (Get-StackOutput "PythonLinuxAmd64ClusterArn") "PythonLinuxAmd64AutoScalingGroupName"
  }
  "linux-arm64" {
    Ensure-WorkerCapacity (Get-StackOutput "PythonLinuxArm64ClusterArn") "PythonLinuxArm64AutoScalingGroupName"
  }
  "windows-only" {
    Ensure-WorkerCapacity (Get-StackOutput "PythonWindowsAmd64ClusterArn") "PythonWindowsAmd64AutoScalingGroupName"
  }
  "linux-only" {
    Ensure-WorkerCapacity (Get-StackOutput "PythonLinuxAmd64ClusterArn") "PythonLinuxAmd64AutoScalingGroupName"
    Ensure-WorkerCapacity (Get-StackOutput "PythonLinuxArm64ClusterArn") "PythonLinuxArm64AutoScalingGroupName"
  }
  default {
    Ensure-WorkerCapacity (Get-StackOutput "PythonLinuxAmd64ClusterArn") "PythonLinuxAmd64AutoScalingGroupName"
    Ensure-WorkerCapacity (Get-StackOutput "PythonLinuxArm64ClusterArn") "PythonLinuxArm64AutoScalingGroupName"
    Ensure-WorkerCapacity (Get-StackOutput "PythonWindowsAmd64ClusterArn") "PythonWindowsAmd64AutoScalingGroupName"
  }
}

$bashArgs = @(
  "--stack-name", $StackName,
  "--input-bucket", $InputBucket,
  "--input-object-key", $InputObjectKey,
  "--evidence-prefix", $EvidencePrefix,
  "--ephemeral-prefix", $EphemeralPrefix,
  "--region", $Region
)

if ($SourceEnvironmentFile -ne "") { $bashArgs += @("--source-environment-file", $SourceEnvironmentFile) }
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
$bashArgs += @("--platform-set", $PlatformSet)

& $bashExe $bashScriptForBash @bashArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
