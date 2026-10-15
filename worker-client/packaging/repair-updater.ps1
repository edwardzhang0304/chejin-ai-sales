param([string]$InstallDirectory = "", [switch]$Restore)
$ErrorActionPreference = 'Stop'
$Utf8 = New-Object System.Text.UTF8Encoding($false)
function Hash([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
$Descriptor = Join-Path $PSScriptRoot 'repair.json'
if ((Hash $Descriptor) -ne 'REPAIR_DESCRIPTOR_SHA256_PLACEHOLDER') { throw 'REPAIR_DESCRIPTOR_CHANGED' }
$Repair = Get-Content -LiteralPath $Descriptor -Raw -Encoding UTF8 | ConvertFrom-Json
$Replacement = Join-Path $PSScriptRoot 'CheJinUpdater.exe'
if ((Hash $Replacement) -ne $Repair.updater_sha256) { throw 'REPAIR_UPDATER_CHANGED' }
if (-not $InstallDirectory) {
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'CheJin Worker.lnk'
    if (Test-Path -LiteralPath $Shortcut) { $InstallDirectory = $Shell.CreateShortcut($Shortcut).WorkingDirectory }
    if (-not $InstallDirectory) {
        Add-Type -AssemblyName System.Windows.Forms
        $Dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $Dialog.Description = '请选择目前车金客户端 EXE 所在文件夹'
        if ($Dialog.ShowDialog() -ne 'OK') { throw 'REPAIR_CANCELLED' }
        $InstallDirectory = $Dialog.SelectedPath
    }
}
$Directory = (Resolve-Path -LiteralPath $InstallDirectory).Path
if ((Get-Item -LiteralPath $Directory).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'REPAIR_LINK_DIRECTORY_NOT_ALLOWED' }
$Worker = Join-Path $Directory 'CheJinWorkerClient.exe'
$Updater = Join-Path $Directory 'CheJinUpdater.exe'
$ManifestPath = Join-Path $Directory 'update-package-manifest.json'
foreach ($File in @($Worker, $Updater, $ManifestPath)) {
    if ((Get-Item -LiteralPath $File).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'REPAIR_LINK_FILE_NOT_ALLOWED' }
}
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$Baseline = @($Repair.baselines | Where-Object { $_.version -eq $Manifest.version })
if ($Baseline.Count -ne 1 -or $Manifest.transport -ne 'hook' -or (Hash $Worker) -ne $Baseline[0].worker_sha256) { throw 'REPAIR_UNRECOGNIZED_INSTALLATION' }
$CurrentHash = Hash $Updater
$Live = @(Get-CimInstance Win32_Process -Filter "Name='CheJinWorkerClient.exe' OR Name='CheJinUpdater.exe'")
foreach ($Process in $Live) {
    # Unknown process paths fail closed. No process is killed by this tool.
    if (-not $Process.ExecutablePath -or [IO.Path]::GetDirectoryName($Process.ExecutablePath) -eq $Directory -or $Process.Name -eq 'CheJinUpdater.exe') {
        throw '请先关闭更新报错框，并正常退出车金 Worker，再运行修复。工作微信可以保持打开。'
    }
}
$Hasher = [Security.Cryptography.SHA256]::Create()
$Id = ([BitConverter]::ToString($Hasher.ComputeHash($Utf8.GetBytes($Directory.ToLowerInvariant())))).Replace('-','').ToLowerInvariant()
$Backup = Join-Path (Join-Path $env:LOCALAPPDATA 'CheJinUpdaterRepair') ($Id + '-' + $Manifest.version)
$ReceiptPath = Join-Path $Backup 'receipt.json'
if ($Restore) {
    $Receipt = Get-Content -LiteralPath $ReceiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($CurrentHash -ne $Repair.updater_sha256 -or (Hash $ManifestPath) -ne $Receipt.patched_manifest_sha256) { throw 'REPAIR_RESTORE_INSTALL_CHANGED' }
    if ((Hash (Join-Path $Backup 'CheJinUpdater.exe')) -ne $Baseline[0].updater_sha256 -or (Hash (Join-Path $Backup 'update-package-manifest.json')) -ne $Baseline[0].manifest_sha256) { throw 'REPAIR_BACKUP_CHANGED' }
    Copy-Item -LiteralPath (Join-Path $Backup 'CheJinUpdater.exe') -Destination $Updater -Force
    Copy-Item -LiteralPath (Join-Path $Backup 'update-package-manifest.json') -Destination $ManifestPath -Force
    Write-Host '原更新器和本地清单已恢复，绑定及业务数据未操作。'
    exit 0
}
if ($CurrentHash -eq $Repair.updater_sha256) {
    $Receipt = Get-Content -LiteralPath $ReceiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ((Hash $ManifestPath) -ne $Receipt.patched_manifest_sha256) { throw 'REPAIR_EXISTING_RECEIPT_MISMATCH' }
    Write-Host '此目录的更新器已经修复，无需重复操作。'
    exit 0
}
if ($CurrentHash -ne $Baseline[0].updater_sha256 -or (Hash $ManifestPath) -ne $Baseline[0].manifest_sha256) { throw 'REPAIR_INSTALLATION_CHANGED' }
New-Item -ItemType Directory -Force -Path $Backup | Out-Null
foreach ($Pair in @(@('CheJinUpdater.exe', $Baseline[0].updater_sha256), @('update-package-manifest.json', $Baseline[0].manifest_sha256))) {
    $Saved = Join-Path $Backup $Pair[0]
    if (Test-Path -LiteralPath $Saved) {
        if ((Hash $Saved) -ne $Pair[1]) { throw 'REPAIR_BACKUP_CONFLICT' }
    } else { Copy-Item -LiteralPath (Join-Path $Directory $Pair[0]) -Destination $Saved }
}
$TempExe = Join-Path $Directory '.CheJinUpdater.repair.tmp'
$TempManifest = Join-Path $Directory '.update-package-manifest.repair.tmp'
try {
    Copy-Item -LiteralPath $Replacement -Destination $TempExe -Force
    if ((Hash $TempExe) -ne $Repair.updater_sha256) { throw 'REPAIR_COPY_CHANGED' }
    $Manifest.files.'CheJinUpdater.exe' = $Repair.updater_sha256
    [IO.File]::WriteAllText($TempManifest, ($Manifest | ConvertTo-Json -Depth 100), $Utf8)
    Move-Item -LiteralPath $TempExe -Destination $Updater -Force
    Move-Item -LiteralPath $TempManifest -Destination $ManifestPath -Force
    $Receipt = @{schema_version=1;version=$Manifest.version;repair_sha256=$Repair.updater_sha256;original_updater_sha256=$Baseline[0].updater_sha256;patched_manifest_sha256=(Hash $ManifestPath);timestamp=[DateTime]::UtcNow.ToString('o')}
    [IO.File]::WriteAllText($ReceiptPath, ($Receipt | ConvertTo-Json), $Utf8)
} catch {
    Copy-Item -LiteralPath (Join-Path $Backup 'CheJinUpdater.exe') -Destination $Updater -Force
    Copy-Item -LiteralPath (Join-Path $Backup 'update-package-manifest.json') -Destination $ManifestPath -Force
    throw
} finally {
    foreach ($Temp in @($TempExe,$TempManifest)) { if (Test-Path -LiteralPath $Temp) { Remove-Item -LiteralPath $Temp -Force } }
}
Write-Host ('更新器已修复（客户端 ' + $Manifest.version + '），没有操作绑定或业务数据库。')
Write-Host '请正常打开原来的 CheJin Worker，在设置里点击“检查更新”。'
