[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$RVersion,

  [Parameter(Mandatory = $false)]
  [string]$InstallRoot = 'C:\R'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$targetDir = Join-Path $InstallRoot "R-$RVersion"
$rscriptPath = Join-Path $targetDir 'bin\Rscript.exe'

if (Get-Command Rscript -ErrorAction SilentlyContinue) {
  $currentVersion = (& Rscript -e "cat(as.character(getRversion()))" 2>$null)
  if ($currentVersion -eq $RVersion) {
    Write-Host "Using existing Rscript in PATH ($currentVersion)"
    exit 0
  }
}

if (Test-Path $rscriptPath) {
  Write-Host "Using cached R runtime at $rscriptPath"
  exit 0
}

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$installerPath = Join-Path $env:TEMP "R-$RVersion-win.exe"
$candidateUris = @(
  "https://cran.r-project.org/bin/windows/base/R-$RVersion-win.exe",
  "https://cran.r-project.org/bin/windows/base/old/$RVersion/R-$RVersion-win.exe"
)

$downloaded = $false
foreach ($uri in $candidateUris) {
  try {
    Invoke-WebRequest -Uri $uri -OutFile $installerPath
    $downloaded = $true
    break
  } catch {
  }
}

if (-not $downloaded) {
  throw "Unable to download R installer for version $RVersion"
}

Start-Process -FilePath $installerPath -ArgumentList '/VERYSILENT',"/DIR=$targetDir" -Wait

if (-not (Test-Path $rscriptPath)) {
  throw "R installation completed but Rscript was not found at $rscriptPath"
}

Write-Host "Installed R $RVersion to $targetDir"
