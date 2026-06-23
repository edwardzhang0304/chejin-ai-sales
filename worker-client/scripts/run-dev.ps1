param(
  [string]$ApiBaseUrl = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Test-Path ".venv")) {
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install --upgrade pip
}

.\.venv\Scripts\pip.exe install -r requirements.txt

Get-ChildItem -Path $Root -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

if ($ApiBaseUrl -ne "") {
  $env:CHEJIN_API_BASE_URL = $ApiBaseUrl
}
.\.venv\Scripts\python.exe -m chejin_worker_client.main
