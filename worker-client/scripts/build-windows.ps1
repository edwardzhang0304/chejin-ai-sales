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
$WorkspaceRoot = Split-Path -Parent $Root
$OmniAutoZipPath = Join-Path $WorkspaceRoot "deliverables\omniauto-add-friend-rpa-pr-candidate-20260618.zip"
$OmniAutoExtractRoot = Join-Path $Root ".tmp-omniauto-rpa"
$OmniAutoSourcePath = Join-Path $OmniAutoExtractRoot "omniauto-add-friend-rpa"
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

if (-not (Test-Path $OmniAutoZipPath)) {
  throw "打包失败：未找到 OmniAuto RPA 候选包 $OmniAutoZipPath"
}
if (Test-Path $OmniAutoExtractRoot) {
  Remove-Item -Recurse -Force $OmniAutoExtractRoot
}
Expand-Archive -Path $OmniAutoZipPath -DestinationPath $OmniAutoExtractRoot -Force
if (-not (Test-Path $OmniAutoSidecarPath)) {
  throw "打包失败：OmniAuto RPA 候选包中未找到 apps\wechat_ai_customer_service\adapters\wechat_win32_ocr_sidecar.py"
}
$env:CHEJIN_OMNIAUTO_RPA_SOURCE = $OmniAutoSourcePath

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
  sidecar_path = $SidecarPath
  file_count = $Files.Count
  package_bytes = $TotalBytes
  preflight_report = if (Test-Path $PreflightReportPath) { $PreflightReportPath } else { $null }
}

$Manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $ManifestPath

Write-Host "Built: $ExePath"
Write-Host "SHA256: $($Hash.Hash)"
Write-Host "Manifest: $ManifestPath"
