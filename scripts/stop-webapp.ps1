$ErrorActionPreference = "Stop"

function Show-Usage {
  @"
Usage: stop-webapp.ps1 [options]

Options:
  --port <port>         (default: 5004)
  --pid-file <path>

PowerShell-style flags also work:
  -Port 5004 -PidFile /tmp/package-scanner-webapp.pid
"@
}

$options = @{
  Port = 5004
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
$bashScript = Join-Path $scriptDir "stop-webapp.sh"
if (-not (Test-Path $bashScript)) {
  throw "Bash script not found: $bashScript"
}

$bashScriptForBash = $bashScript -replace "\\", "/"
if ($bashScriptForBash -match "^([A-Za-z]):/(.*)$") {
  $bashScriptForBash = "/" + $Matches[1].ToLowerInvariant() + "/" + $Matches[2]
}

$bashArgs = @(
  "--port", $options.Port.ToString()
)

if ($options.PidFile -ne "") { $bashArgs += @("--pid-file", $options.PidFile) }

& $bashExe $bashScriptForBash @bashArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
