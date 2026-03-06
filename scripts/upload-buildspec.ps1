param(
  [Parameter(Mandatory = $true)]
  [string]$ResultsBucket,

  [Parameter(Mandatory = $false)]
  [string]$Region = "us-east-1",

  [Parameter(Mandatory = $false)]
  [string]$Profile = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$buildspecPath = Join-Path $repoRoot "deployment\buildspecs\python-scan-buildspec.yml"

if (-not (Test-Path $buildspecPath)) {
  throw "Buildspec not found: $buildspecPath"
}

$awsArgs = @("--region", $Region)
if ($Profile -ne "") {
  $awsArgs += @("--profile", $Profile)
}

Write-Host "Uploading buildspec to s3://$ResultsBucket/config/python-scan-buildspec.yml ..."
aws s3 cp $buildspecPath "s3://$ResultsBucket/config/python-scan-buildspec.yml" @awsArgs

Write-Host "Buildspec upload complete."

