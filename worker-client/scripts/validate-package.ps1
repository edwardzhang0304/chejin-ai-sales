param(
  [string]$PackageDir = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if ($PackageDir -eq "") {
  $PackageDir = Join-Path $Root "dist\CheJinWorkerClient"
}

$ExePath = Join-Path $PackageDir "CheJinWorkerClient.exe"
$UpdaterExePath = Join-Path $PackageDir "CheJinUpdater.exe"
$UpdatePackageManifestPath = Join-Path $PackageDir "update-package-manifest.json"
if (-not (Test-Path $ExePath)) {
  throw "产物校验失败：未找到 $ExePath"
}
if (-not (Test-Path $UpdaterExePath)) {
  throw "产物校验失败：未找到 $UpdaterExePath"
}
if (-not (Test-Path $UpdatePackageManifestPath)) {
  throw "产物校验失败：未找到 $UpdatePackageManifestPath"
}
$UpdatePackageManifest = Get-Content -Raw -Encoding UTF8 $UpdatePackageManifestPath | ConvertFrom-Json
if ($UpdatePackageManifest.platform -ne "windows-x64" -or $UpdatePackageManifest.rollback_safe -ne $true) {
  throw "产物校验失败：自动更新包内清单平台或回滚声明无效"
}
$ExpectedUpdateFiles = @{}
foreach ($Property in $UpdatePackageManifest.files.PSObject.Properties) {
  $ExpectedUpdateFiles[$Property.Name] = [string]$Property.Value
}
$ActualUpdateFiles = @(Get-ChildItem $PackageDir -Recurse -File | Where-Object { $_.FullName -ne $UpdatePackageManifestPath })
if ($ExpectedUpdateFiles.Count -ne $ActualUpdateFiles.Count) {
  throw "产物校验失败：自动更新包内清单文件数量不一致"
}
foreach ($File in $ActualUpdateFiles) {
  $RelativePath = $File.FullName.Substring($PackageDir.TrimEnd("\").Length + 1).Replace("\", "/")
  if (-not $ExpectedUpdateFiles.ContainsKey($RelativePath)) {
    throw "产物校验失败：自动更新包内清单漏记 $RelativePath"
  }
  if ((Get-FileHash -Algorithm SHA256 $File.FullName).Hash.ToLowerInvariant() -ne $ExpectedUpdateFiles[$RelativePath].ToLowerInvariant()) {
    throw "产物校验失败：自动更新包内文件哈希不一致 $RelativePath"
  }
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
$UpdaterHash = Get-FileHash -Algorithm SHA256 $UpdaterExePath
$TotalBytes = ($Files | Measure-Object -Property Length -Sum).Sum

$Report = [ordered]@{
  ok = $true
  package_dir = $PackageDir
  exe_path = $ExePath
  exe_sha256 = $Hash.Hash
  updater_exe_path = $UpdaterExePath
  updater_exe_sha256 = $UpdaterHash.Hash
  update_package_manifest_path = $UpdatePackageManifestPath
  sidecar_path = $SidecarPath
  file_count = $Files.Count
  package_bytes = $TotalBytes
  checked_at = (Get-Date).ToUniversalTime().ToString("o")
}

$Report | ConvertTo-Json -Depth 5
