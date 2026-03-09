#!/usr/bin/env pwsh
[CmdletBinding()]
param ()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# -----------------------------------------------------------------------------
# Configuration (edit before running)
# -----------------------------------------------------------------------------
$Profile = 'admin'
$Region = 'us-east-1'
$StackName = 'package-scanner-dev'
$EnvironmentName = 'package-scanner-dev'
$InputBucket = 'natgo-projects'
$TemplateS3Bucket = $InputBucket
$DeploymentLockToken = 'dev-lock-2026-03'
$ExpectedAccountId = '123456789012'
$PythonEnvFile = 'environment.yml'
$PythonInputS3Key = 'package-scanner/inputs/python/environment.yml' # S3 object key (remote path)
$REnvFile = 'renv.lock'
$RInputKey = 'inputs/r/renv.lock'
$SafetyApiKey = ''
# -----------------------------------------------------------------------------

function Require-File {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Required file not found: $Path"
  }
}

function Invoke-Step {
  param([string]$Message, [ScriptBlock]$Action)
  Write-Host "==> $Message"
  & $Action
}

Require-File $PythonEnvFile
Require-File $REnvFile

Invoke-Step "Deploying/refreshing CloudFormation stack" {
  ./scripts/deploy-cfn.sh `
    --region $Region `
    --profile $Profile `
    --expected-account-id $ExpectedAccountId `
    --deployment-lock-token $DeploymentLockToken `
    --stack-name $StackName `
    --environment-name $EnvironmentName `
    --s3-bucket $TemplateS3Bucket
}

Invoke-Step "Uploading Python environment to s3://$InputBucket/$PythonInputS3Key" {
  aws s3 cp $PythonEnvFile "s3://$InputBucket/$PythonInputS3Key" `
    --region $Region `
    --profile $Profile
}

Invoke-Step "Uploading R lockfile to s3://$InputBucket/$RInputKey" {
  aws s3 cp $REnvFile "s3://$InputBucket/$RInputKey" `
    --region $Region `
    --profile $Profile
}

Invoke-Step "Starting Python (numpy) smoke scan" {
  ./scripts/start-python-scan.sh `
    --stack-name $StackName `
    --input-bucket $InputBucket `
    --input-object-key $PythonInputS3Key `
    --region $Region `
    --profile $Profile `
    --expected-account-id $ExpectedAccountId `
    --deployment-lock-token $DeploymentLockToken `
    --remediate-medium true `
    --fail-on-medium false `
    --safety-api-key $SafetyApiKey
}

Invoke-Step "Starting R (tidyverse) smoke scan" {
  ./scripts/start-r-scan.sh `
    --stack-name $StackName `
    --input-bucket $InputBucket `
    --input-object-key $RInputKey `
    --region $Region `
    --profile $Profile `
    --expected-account-id $ExpectedAccountId `
    --deployment-lock-token $DeploymentLockToken `
    --remediate-medium true `
    --fail-on-medium false
}

Write-Host "All AWS actions submitted. Monitor CodeBuild for completion."
