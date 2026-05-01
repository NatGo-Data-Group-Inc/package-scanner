param(
  [Parameter(Mandatory = $true)]
  [string]$StackName,

  [Parameter(Mandatory = $false)]
  [string]$Region = "us-east-1",

  [Parameter(Mandatory = $false)]
  [string]$Profile = "",

  [Parameter(Mandatory = $false)]
  [switch]$AllowDefaultProfile,

  [Parameter(Mandatory = $false)]
  [string]$Tag = "",

  [Parameter(Mandatory = $false)]
  [switch]$NoLatest
)

$ErrorActionPreference = "Stop"

if (-not $AllowDefaultProfile -and $Profile -eq "") {
  throw "Guardrail: -Profile is required unless -AllowDefaultProfile is explicitly set."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$dockerfile = Join-Path $repoRoot "docker\python-windows.Dockerfile"
if (-not (Test-Path $dockerfile)) {
  throw "Dockerfile not found: $dockerfile"
}

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
  throw "aws CLI is required in PATH."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw "docker is required in PATH."
}

$awsArgs = @("--region", $Region)
if ($Profile -ne "") {
  $awsArgs += @("--profile", $Profile)
}

function Get-StackOutput {
  param([string]$Key)
  $value = aws cloudformation describe-stacks `
    --stack-name $StackName `
    --query "Stacks[0].Outputs[?OutputKey=='$Key'].OutputValue | [0]" `
    --output text @awsArgs
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to query stack output: $Key"
  }
  return $value
}

$repositoryUri = Get-StackOutput "PythonWindowsRepositoryUri"
if ([string]::IsNullOrWhiteSpace($repositoryUri) -or $repositoryUri -eq "None") {
  throw "Missing output: PythonWindowsRepositoryUri"
}

if ([string]::IsNullOrWhiteSpace($Tag)) {
  $gitSha = ""
  if (Get-Command git -ErrorAction SilentlyContinue) {
    $gitSha = (git rev-parse --short=12 HEAD 2>$null)
  }
  $timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
  if (-not [string]::IsNullOrWhiteSpace($gitSha)) {
    $Tag = "$timestamp-$gitSha"
  } else {
    $Tag = $timestamp
  }
}

$accountId = aws sts get-caller-identity --query Account --output text @awsArgs
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($accountId) -or $accountId -eq "None") {
  throw "Unable to resolve AWS account id."
}

$loginPassword = aws ecr get-login-password @awsArgs
if ($LASTEXITCODE -ne 0) {
  throw "Failed to retrieve ECR login password."
}
$loginPassword | docker login --username AWS --password-stdin "$accountId.dkr.ecr.$Region.amazonaws.com"
if ($LASTEXITCODE -ne 0) {
  throw "Docker login to ECR failed."
}

$repoRootPath = $repoRoot
$tagArgs = @("-t", "${repositoryUri}:${Tag}")
if (-not $NoLatest) {
  $tagArgs += @("-t", "${repositoryUri}:latest")
}

docker build -f $dockerfile @tagArgs $repoRootPath
if ($LASTEXITCODE -ne 0) {
  throw "Windows Python image build failed."
}

docker push "${repositoryUri}:${Tag}"
if ($LASTEXITCODE -ne 0) {
  throw "Failed to push image tag ${Tag}."
}

if (-not $NoLatest) {
  docker push "${repositoryUri}:latest"
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to push latest tag."
  }
}

Write-Host "Python Windows image pushed: ${repositoryUri}:${Tag}"
if (-not $NoLatest) {
  Write-Host "Python Windows latest tag refreshed: ${repositoryUri}:latest"
}
