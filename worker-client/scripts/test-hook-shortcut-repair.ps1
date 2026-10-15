# Isolated Windows check: discover the installation from real .lnk files.
# Sentinel EXEs are never executed; only the existing repair/restore code runs.
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ("hook shortcut's " + [guid]::NewGuid())
$program = Join-Path $root 'installed'
$patch = Join-Path $root 'patch'
$desktop = [Environment]::GetFolderPath('Desktop')
$linkPath = Join-Path $desktop 'CheJin Worker.lnk'
if (Test-Path -LiteralPath $linkPath) { throw 'Test shortcut already exists' }
$originalLocalData = $env:LOCALAPPDATA
$utf8 = New-Object System.Text.UTF8Encoding($false)
function Hash([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
try {
    New-Item -ItemType Directory -Path $program, $patch, $desktop -Force | Out-Null
    $env:LOCALAPPDATA = Join-Path $root 'local-data'
    $worker = Join-Path $program 'CheJinWorkerClient.exe'
    $updater = Join-Path $program 'CheJinUpdater.exe'
    $manifest = Join-Path $program 'update-package-manifest.json'
    [IO.File]::WriteAllText($worker, 'worker-sentinel', $utf8)
    [IO.File]::WriteAllText((Join-Path $program 'start-hook.ps1'), '# never executed', $utf8)
    $replacement = Join-Path $patch 'CheJinUpdater.exe'
    [IO.File]::WriteAllText($replacement, 'replacement-sentinel', $utf8)
    [IO.File]::WriteAllText($updater, 'original-sentinel', $utf8)
    $originalUpdater = Hash $updater
    $originalManifest = @{version='fixture-version';transport='hook';files=@{'CheJinUpdater.exe'=$originalUpdater}} | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText($manifest, $originalManifest, $utf8)
    $descriptor = @{updater_sha256=(Hash $replacement);baselines=@(@{version='fixture-version';worker_sha256=(Hash $worker);updater_sha256=$originalUpdater;manifest_sha256=(Hash $manifest)})}
    $descriptorPath = Join-Path $patch 'repair.json'
    [IO.File]::WriteAllText($descriptorPath, ($descriptor | ConvertTo-Json -Depth 5), $utf8)
    $repair = Join-Path $patch 'repair-updater.ps1'
    $source = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../packaging/repair-updater.ps1') -Raw -Encoding UTF8
    [IO.File]::WriteAllText($repair, $source.Replace('REPAIR_DESCRIPTOR_SHA256_PLACEHOLDER', (Hash $descriptorPath)), (New-Object System.Text.UTF8Encoding($true)))
    foreach ($mode in @('new-safe-directory', 'legacy-working-directory')) {
        $link = (New-Object -ComObject WScript.Shell).CreateShortcut($linkPath)
        $link.TargetPath = Join-Path $PSHOME 'powershell.exe'
        if ($mode -eq 'new-safe-directory') {
            $link.WorkingDirectory = [IO.Path]::GetTempPath()
            $link.Arguments = '-NoProfile -WindowStyle Hidden -File "' + (Join-Path $program 'start-hook.ps1') + '" -ShowErrorDialog'
        } else {
            $link.WorkingDirectory = $program
            $link.Arguments = '' # Old directory fallback is still supported.
        }
        $link.Save()
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $repair
        if ($LASTEXITCODE -ne 0 -or (Hash $updater) -ne (Hash $replacement)) { throw ('Shortcut repair discovery failed: ' + $mode) }
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $repair -Restore
        if ($LASTEXITCODE -ne 0 -or (Hash $updater) -ne $originalUpdater -or (Get-Content -LiteralPath $manifest -Raw) -ne $originalManifest) { throw ('Shortcut repair restoration failed: ' + $mode) }
        Write-Host ('PASS updater repair and restore discover installation from ' + $mode)
    }
} finally {
    $env:LOCALAPPDATA = $originalLocalData
    Remove-Item -LiteralPath $linkPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
