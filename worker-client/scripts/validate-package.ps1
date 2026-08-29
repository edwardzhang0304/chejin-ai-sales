param(
  [string]$PackageDir = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if ($PackageDir -eq "") {
  $PackageDir = Join-Path $Root "dist\CheJinWorkerClient"
}

$ExePath = Join-Path $PackageDir "CheJinWorkerClient.exe"
if (-not (Test-Path $ExePath)) {
  throw "产物校验失败：未找到 $ExePath"
}

$SidecarCandidates = @(
  (Join-Path $PackageDir "_internal\omniauto-rpa\apps\wechat_ai_customer_service\adapters\wechat_win32_ocr_sidecar.py"),
  (Join-Path $PackageDir "omniauto-rpa\apps\wechat_ai_customer_service\adapters\wechat_win32_ocr_sidecar.py")
)
$SidecarPath = $SidecarCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $SidecarPath) {
  throw "产物校验失败：未找到 OmniAuto wechat_win32_ocr_sidecar.py"
}

$Files = Get-ChildItem $PackageDir -Recurse -File
$Hash = Get-FileHash -Algorithm SHA256 $ExePath
$TotalBytes = ($Files | Measure-Object -Property Length -Sum).Sum

$Report = [ordered]@{
  ok = $true
  package_dir = $PackageDir
  exe_path = $ExePath
  exe_sha256 = $Hash.Hash
  sidecar_path = $SidecarPath
  file_count = $Files.Count
  package_bytes = $TotalBytes
  checked_at = (Get-Date).ToUniversalTime().ToString("o")
}

$Report | ConvertTo-Json -Depth 5
