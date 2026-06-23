param(
  [string]$ApiBaseUrl = "",
  [string]$WorkerHome = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Test-Path ".venv")) {
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install --upgrade pip
  .\.venv\Scripts\pip.exe install -r requirements.txt
}

$env:CHEJIN_RPA_MODE = "real"
if ($ApiBaseUrl -ne "") {
  $env:CHEJIN_API_BASE_URL = $ApiBaseUrl
}
if ($WorkerHome -ne "") {
  $env:CHEJIN_WORKER_HOME = $WorkerHome
}

.\.venv\Scripts\python.exe -m chejin_worker_client.main --wechat-diagnostics
