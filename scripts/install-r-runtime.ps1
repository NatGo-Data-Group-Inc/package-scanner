[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$RVersion,

  [Parameter(Mandatory = $false)]
  [string]$InstallRoot = 'C:\R',

  [Parameter(Mandatory = $false)]
  [switch]$EnsureRtools,

  [Parameter(Mandatory = $false)]
  [string]$RtoolsVersion = 'rtools44-6459-6401.exe',

  [Parameter(Mandatory = $false)]
  [string]$RtoolsInstallRoot = 'C:\rtools44',

  [Parameter(Mandatory = $false)]
  [string]$EvidenceBucket = '',

  [Parameter(Mandatory = $false)]
  [string]$RtoolsInstallerS3Key = 'config/rtools/rtools44-6459-6401.exe',

  [Parameter(Mandatory = $false)]
  [switch]$SkipRRuntime
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-DownloadWithRetry {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Uri,

    [Parameter(Mandatory = $true)]
    [string]$OutFile,

    [Parameter(Mandatory = $false)]
    [int]$Attempts = 3
  )

  for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
    try {
      if (Test-Path $OutFile) {
        Remove-Item -Force $OutFile
      }
      Invoke-WebRequest -Uri $Uri -OutFile $OutFile
      if ((Test-Path $OutFile) -and ((Get-Item $OutFile).Length -gt 0)) {
        return
      }
    } catch {
      if ($attempt -eq $Attempts) {
        throw
      }
      Start-Sleep -Seconds ([Math]::Min(10 * $attempt, 30))
    }
  }
}

function Ensure-RtoolsInstall {
  param(
    [string]$InstallRoot,
    [string]$InstallerName,
    [string]$Bucket,
    [string]$InstallerS3Key
  )

  if (Test-Path $InstallRoot) {
    Write-Host "Using existing Rtools at $InstallRoot"
    return
  }

  $installerPath = Join-Path $env:TEMP $InstallerName
  $downloaded = $false

  if ($Bucket) {
    try {
      aws s3 cp "s3://$Bucket/$InstallerS3Key" $installerPath | Out-Null
      if ((Test-Path $installerPath) -and ((Get-Item $installerPath).Length -gt 0)) {
        $downloaded = $true
        Write-Host "Downloaded Rtools installer from s3://$Bucket/$InstallerS3Key"
      }
    } catch {
    }
  }

  if (-not $downloaded) {
    $candidateUris = @(
      "https://cran.r-project.org/bin/windows/Rtools/rtools44/files/$InstallerName",
      "https://cloud.r-project.org/bin/windows/Rtools/rtools44/files/$InstallerName"
    )
    foreach ($uri in $candidateUris) {
      try {
        Invoke-DownloadWithRetry -Uri $uri -OutFile $installerPath
        $downloaded = $true
        Write-Host "Downloaded Rtools installer from $uri"
        break
      } catch {
      }
    }
  }

  if (-not $downloaded) {
    throw "Unable to download Rtools installer $InstallerName"
  }

  Start-Process -FilePath $installerPath -ArgumentList '/VERYSILENT',"/DIR=$InstallRoot" -Wait

  if (-not (Test-Path (Join-Path $InstallRoot 'usr\bin'))) {
    throw "Rtools installation completed but expected files were not found under $InstallRoot"
  }

  if ($Bucket) {
    try {
      aws s3 cp $installerPath "s3://$Bucket/$InstallerS3Key" | Out-Null
    } catch {
    }
  }
}

$targetDir = Join-Path $InstallRoot "R-$RVersion"
$rscriptPath = Join-Path $targetDir 'bin\Rscript.exe'

if ($EnsureRtools) {
  Ensure-RtoolsInstall -InstallRoot $RtoolsInstallRoot -InstallerName $RtoolsVersion -Bucket $EvidenceBucket -InstallerS3Key $RtoolsInstallerS3Key
}

if ($SkipRRuntime) {
  exit 0
}

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
