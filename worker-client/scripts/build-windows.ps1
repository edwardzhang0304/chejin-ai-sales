param(
  [switch]$SkipTests,
  [switch]$SkipPreflight,
  [string]$ApiBaseUrl = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$ReportsDir = Join-Path $Root "dist\reports"
$PackageDir = Join-Path $Root "dist\车金Worker客户端"
$ExePath = Join-Path $PackageDir "车金Worker客户端.exe"
$ManifestPath = Join-Path $ReportsDir "车金Worker客户端.manifest.json"
$PreflightReportPath = Join-Path $ReportsDir "preflight-build-report.json"
$OmniAutoUpstreamCommit = "855c21881641cdb2f9fe69d3f2e1caa05e37d04d"
$OmniAutoSourcePath = Join-Path $Root "omniauto-rpa"
$OmniAutoSidecarPath = Join-Path $OmniAutoSourcePath "apps\wechat_ai_customer_service\adapters\wechat_win32_ocr_sidecar.py"

if (-not (Test-Path ".venv")) {
  python -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r requirements.txt

if (-not $SkipTests) {
  .\.venv\Scripts\python.exe run_checks.py
}

New-Item -ItemType Directory -Force -Path $ReportsDir | Out-Null

if (-not $SkipPreflight) {
  $env:CHEJIN_RPA_MODE = "mock"
  $env:CHEJIN_WORKER_HOME = Join-Path $Root ".tmp-build-preflight"
  if ($ApiBaseUrl -ne "") {
    $env:CHEJIN_API_BASE_URL = $ApiBaseUrl
  }
  .\.venv\Scripts\python.exe -m chejin_worker_client.main --preflight --skip-backend --skip-wechat --preflight-format json --write-report $PreflightReportPath
}

if (-not (Test-Path $OmniAutoSidecarPath)) {
  throw "打包失败：当前分支未找到 OmniAuto sidecar $OmniAutoSidecarPath"
}
$env:CHEJIN_OMNIAUTO_RPA_SOURCE = $OmniAutoSourcePath
$OmniAutoSourceSidecarHash = Get-FileHash -Algorithm SHA256 $OmniAutoSidecarPath

.\.venv\Scripts\pyinstaller.exe --clean --noconfirm packaging\chejin-worker-client.spec

if (-not (Test-Path $ExePath)) {
  throw "打包失败：未找到 $ExePath"
}

$SidecarCandidates = @(
  (Join-Path $PackageDir "_internal\omniauto-rpa\apps\wechat_ai_customer_service\adapters\wechat_win32_ocr_sidecar.py"),
  (Join-Path $PackageDir "omniauto-rpa\apps\wechat_ai_customer_service\adapters\wechat_win32_ocr_sidecar.py")
)
$SidecarPath = $SidecarCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $SidecarPath) {
  throw "打包失败：dist 产物中未找到 OmniAuto wechat_win32_ocr_sidecar.py"
}
$PackagedSidecarHash = Get-FileHash -Algorithm SHA256 $SidecarPath
if ($PackagedSidecarHash.Hash -ne $OmniAutoSourceSidecarHash.Hash) {
  throw "打包失败：dist 中 OmniAuto sidecar 与当前分支源码不一致"
}

$Hash = Get-FileHash -Algorithm SHA256 $ExePath
$Files = Get-ChildItem $PackageDir -Recurse -File
$TotalBytes = ($Files | Measure-Object -Property Length -Sum).Sum
$Version = .\.venv\Scripts\python.exe -c "from chejin_worker_client import __version__; print(__version__)"

$Manifest = [ordered]@{
  app_name = "车金Worker客户端"
  version = $Version.Trim()
  built_at = (Get-Date).ToUniversalTime().ToString("o")
  package_dir = $PackageDir
  exe_path = $ExePath
  exe_sha256 = $Hash.Hash
  omniauto_upstream_commit = $OmniAutoUpstreamCommit
  omniauto_source_path = $OmniAutoSourcePath
  omniauto_source_sidecar_sha256 = $OmniAutoSourceSidecarHash.Hash
  sidecar_path = $SidecarPath
  packaged_sidecar_sha256 = $PackagedSidecarHash.Hash
  file_count = $Files.Count
  package_bytes = $TotalBytes
  preflight_report = if (Test-Path $PreflightReportPath) { $PreflightReportPath } else { $null }
}

$Manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $ManifestPath

Write-Host "Built: $ExePath"
Write-Host "SHA256: $($Hash.Hash)"
Write-Host "Manifest: $ManifestPath"
