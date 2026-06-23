param(
  [ValidateSet("real", "mock")]
  [string]$RpaMode = "real",
  [switch]$SkipBackend,
  [switch]$SkipWechat,
  [string]$ApiBaseUrl = "",
  [string]$ReportPath = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Test-Path ".venv")) {
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install --upgrade pip
  .\.venv\Scripts\pip.exe install -r requirements.txt
}

$env:CHEJIN_RPA_MODE = $RpaMode
if ($ApiBaseUrl -ne "") {
  $env:CHEJIN_API_BASE_URL = $ApiBaseUrl
}

$argsList = @("-m", "chejin_worker_client.main", "--preflight")
if ($SkipBackend) {
  $argsList += "--skip-backend"
}
if ($SkipWechat) {
  $argsList += "--skip-wechat"
}
if ($ReportPath -ne "") {
  $argsList += @("--write-report", $ReportPath)
}

.\.venv\Scripts\python.exe @argsList
