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
$REnvFile = 'artifacts/renv.lock'
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
    --source-lock-file $REnvFile `
    --region $Region `
    --profile $Profile `
    --expected-account-id $ExpectedAccountId `
    --deployment-lock-token $DeploymentLockToken `
    --remediate-medium true `
    --fail-on-medium false
}

Write-Host "All AWS actions submitted. Monitor the R Step Functions execution and CodeBuild child runs for completion."
