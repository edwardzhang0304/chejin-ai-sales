$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Test-Path ".venv")) {
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install --upgrade pip
  .\.venv\Scripts\pip.exe install -r requirements.txt
}

$env:CHEJIN_RPA_MODE = "mock"
$env:CHEJIN_RPA_MOCK_STEP_DELAY_SECONDS = "0"
$env:CHEJIN_WORKER_HOME = Join-Path $Root ".tmp-checks"
.\.venv\Scripts\python.exe -W "error::ResourceWarning" -m unittest discover -s tests -v
.\.venv\Scripts\python.exe smoke_e2e.py
.\.venv\Scripts\python.exe -m compileall chejin_worker_client
