$ErrorActionPreference = "Stop"

function Show-Usage {
  @"
Usage: start-webapp.ps1 [options]

Options:
  --port <port>                 (default: 5004)
  --host <host>                 (default: 127.0.0.1)
  --region <region>             (default: us-east-1)
  --profile <profile>
  --allow-default-profile
  --catalog-bucket <bucket>
  --catalog-prefix <prefix>     (default: evidence)
  --ephemeral-bucket <bucket>
  --ephemeral-prefix <prefix>   (default: deploy/tmp/r)
  --app-home <dir>
  --log-file <path>
  --pid-file <path>

PowerShell-style flags also work:
  -Port 5004 -Profile my-profile -CatalogBucket my-bucket
"@
}

$options = @{
  Port = 5004
  Host = "127.0.0.1"
  Region = "us-east-1"
  Profile = ""
  AllowDefaultProfile = $false
  CatalogBucket = ""
  CatalogPrefix = "evidence"
  EphemeralBucket = ""
  EphemeralPrefix = "deploy/tmp/r"
  AppHome = ""
  LogFile = ""
  PidFile = ""
}

$argsList = @($args)
for ($i = 0; $i -lt $argsList.Count; $i++) {
  $arg = [string]$argsList[$i]
  switch -Regex ($arg) {
    '^(--?h|--?help)$' {
      Show-Usage
      exit 0
    }
    '^--port$|^-Port$' {
      $i++
      if ($i -ge $argsList.Count) { throw "Missing value for $arg" }
      $options.Port = [int]$argsList[$i]
    }
    '^--host$|^-Host$' {
      $i++
      if ($i -ge $argsList.Count) { throw "Missing value for $arg" }
      $options.Host = [string]$argsList[$i]
    }
    '^--region$|^-Region$' {
      $i++
      if ($i -ge $argsList.Count) { throw "Missing value for $arg" }
      $options.Region = [string]$argsList[$i]
    }
    '^--profile$|^-Profile$' {
      $i++
      if ($i -ge $argsList.Count) { throw "Missing value for $arg" }
      $options.Profile = [string]$argsList[$i]
    }
    '^--allow-default-profile$|^-AllowDefaultProfile$' {
      $options.AllowDefaultProfile = $true
    }
    '^--catalog-bucket$|^-CatalogBucket$' {
      $i++
      if ($i -ge $argsList.Count) { throw "Missing value for $arg" }
      $options.CatalogBucket = [string]$argsList[$i]
    }
    '^--catalog-prefix$|^-CatalogPrefix$' {
      $i++
      if ($i -ge $argsList.Count) { throw "Missing value for $arg" }
      $options.CatalogPrefix = [string]$argsList[$i]
    }
    '^--ephemeral-bucket$|^-EphemeralBucket$' {
      $i++
      if ($i -ge $argsList.Count) { throw "Missing value for $arg" }
      $options.EphemeralBucket = [string]$argsList[$i]
    }
    '^--ephemeral-prefix$|^-EphemeralPrefix$' {
      $i++
      if ($i -ge $argsList.Count) { throw "Missing value for $arg" }
      $options.EphemeralPrefix = [string]$argsList[$i]
    }
    '^--app-home$|^-AppHome$' {
      $i++
      if ($i -ge $argsList.Count) { throw "Missing value for $arg" }
      $options.AppHome = [string]$argsList[$i]
    }
    '^--log-file$|^-LogFile$' {
      $i++
      if ($i -ge $argsList.Count) { throw "Missing value for $arg" }
      $options.LogFile = [string]$argsList[$i]
    }
    '^--pid-file$|^-PidFile$' {
      $i++
      if ($i -ge $argsList.Count) { throw "Missing value for $arg" }
      $options.PidFile = [string]$argsList[$i]
    }
    default {
      throw "Unknown argument: $arg"
    }
  }
}

$bashExe = "bash"
$gitBash = "C:\Program Files\Git\bin\bash.exe"
if (Test-Path $gitBash) {
  $bashExe = $gitBash
} elseif (-not (Get-Command bash -ErrorAction SilentlyContinue)) {
  throw "bash is required in PATH to run this wrapper."
}

$scriptDir = Split-Path -Parent $PSCommandPath
$bashScript = Join-Path $scriptDir "start-webapp.sh"
if (-not (Test-Path $bashScript)) {
  throw "Bash script not found: $bashScript"
}

$bashScriptForBash = $bashScript -replace "\\", "/"
if ($bashScriptForBash -match "^([A-Za-z]):/(.*)$") {
  $bashScriptForBash = "/" + $Matches[1].ToLowerInvariant() + "/" + $Matches[2]
}

$bashArgs = @(
  "--port", $options.Port.ToString(),
  "--host", $options.Host,
  "--region", $options.Region,
  "--catalog-prefix", $options.CatalogPrefix,
  "--ephemeral-prefix", $options.EphemeralPrefix
)

if ($options.Profile -ne "") { $bashArgs += @("--profile", $options.Profile) }
if ($options.AllowDefaultProfile) { $bashArgs += "--allow-default-profile" }
if ($options.CatalogBucket -ne "") { $bashArgs += @("--catalog-bucket", $options.CatalogBucket) }
if ($options.EphemeralBucket -ne "") { $bashArgs += @("--ephemeral-bucket", $options.EphemeralBucket) }
if ($options.AppHome -ne "") { $bashArgs += @("--app-home", $options.AppHome) }
if ($options.LogFile -ne "") { $bashArgs += @("--log-file", $options.LogFile) }
if ($options.PidFile -ne "") { $bashArgs += @("--pid-file", $options.PidFile) }

& $bashExe $bashScriptForBash @bashArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
