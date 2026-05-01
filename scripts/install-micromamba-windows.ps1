$ErrorActionPreference = "Stop"

Invoke-WebRequest -Uri https://micro.mamba.pm/api/micromamba/win-64/latest -OutFile C:\micromamba.tar.bz2
New-Item -ItemType Directory -Force -Path C:\micromamba | Out-Null

& python @(
  "-c",
  "import tarfile; tarfile.open(r'C:\micromamba.tar.bz2', 'r:bz2').extractall(r'C:\micromamba')"
)
if ($LASTEXITCODE -ne 0) {
  throw "micromamba extraction failed"
}

Remove-Item C:\micromamba.tar.bz2 -Force
if (-not (Test-Path "C:\micromamba\Library\bin\micromamba.exe")) {
  throw "micromamba.exe not found after extraction"
}
