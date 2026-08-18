param(
  [Parameter(Mandatory = $true)]
  [string]$ApiBaseUrl,
  [switch]$PreflightOnly,
  [switch]$SkipBackend,
  [switch]$SkipWechat
)

$ErrorActionPreference = "Stop"
$parsedUrl = $null
if (-not [Uri]::TryCreate($ApiBaseUrl, [UriKind]::Absolute, [ref]$parsedUrl) -or
    $parsedUrl.Scheme -notin @("http", "https")) {
  throw "ApiBaseUrl must be a complete http:// or https:// URL."
}

$packageRoot = $PSScriptRoot
$runtimeRoot = Join-Path $packageRoot "runtime"
$appRoot = Join-Path $packageRoot "app"
$pythonExe = Join-Path $runtimeRoot "python.exe"
$pythonwExe = Join-Path $runtimeRoot "pythonw.exe"
$identityPath = Join-Path $appRoot "runtime-build-identity.json"
$visionCredentialPath = Join-Path $appRoot "vision-runtime.json"
$omniautoPath = Join-Path $appRoot "omniauto-rpa"

foreach ($requiredPath in @($pythonExe, $pythonwExe, $identityPath, $visionCredentialPath, $omniautoPath)) {
  if (-not (Test-Path $requiredPath)) {
    throw "Fast UAT package is incomplete: $requiredPath"
  }
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = "$appRoot;$runtimeRoot\Lib\site-packages"
$env:CHEJIN_API_BASE_URL = $ApiBaseUrl.TrimEnd("/")
$env:CHEJIN_RPA_MODE = "real"
$env:CHEJIN_BUILD_KIND = "debug_uat_locked"
$env:CHEJIN_BUILD_IDENTITY_PATH = $identityPath
$env:CHEJIN_VISION_CREDENTIAL_PATH = $visionCredentialPath
$env:CHEJIN_OMNIAUTO_RPA_SOURCE = $omniautoPath

$localAppData = [Environment]::GetFolderPath("LocalApplicationData")
if ([string]::IsNullOrWhiteSpace($localAppData)) {
  throw "LOCALAPPDATA is unavailable."
}
$diagnosticsDir = Join-Path $localAppData "CheJinWorker\diagnostics"
New-Item -ItemType Directory -Force -Path $diagnosticsDir | Out-Null
$reportPath = Join-Path $diagnosticsDir ("fast-uat-preflight-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".json")

$preflightArgs = @(
  "-m", "chejin_worker_client.main",
  "--preflight", "--preflight-format", "json", "--write-report", $reportPath
)
if ($SkipBackend) { $preflightArgs += "--skip-backend" }
if ($SkipWechat) { $preflightArgs += "--skip-wechat" }

$preflight = Start-Process -FilePath $pythonExe -ArgumentList $preflightArgs -Wait -PassThru
if ($preflight.ExitCode -ne 0) {
  Write-Host "Fast UAT preflight failed. Client was not started." -ForegroundColor Red
  Write-Host "Report: $reportPath"
  exit $preflight.ExitCode
}

Write-Host "Fast UAT preflight passed." -ForegroundColor Green
Write-Host "API: $($env:CHEJIN_API_BASE_URL)"
Write-Host "Report: $reportPath"
if (-not $PreflightOnly) {
  Start-Process -FilePath $pythonwExe -ArgumentList @("-m", "chejin_worker_client.main") -WorkingDirectory $packageRoot
}
