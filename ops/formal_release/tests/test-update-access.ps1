$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('chejin-access-test-' + [guid]::NewGuid().ToString('N'))
$localData = Join-Path $root 'local'
$requests = Join-Path $localData 'CheJinWorkerUpdate/requests'
$tool = Join-Path $root 'tool'
New-Item -ItemType Directory -Path $requests,$tool -Force | Out-Null
$original = Join-Path $requests 'update-synthetic-original'
$stage = Join-Path $original 'staging.extracting'
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$sentinel = Join-Path $stage 'do-not-modify.txt'
[IO.File]::WriteAllText($sentinel, 'unchanged-existing-cache')
$source = Join-Path $PSScriptRoot '../diagnose-update-access.ps1'
Copy-Item -LiteralPath $source -Destination $tool
$saved = $env:LOCALAPPDATA
$held = $null
try {
    $env:LOCALAPPDATA = $localData
    $held = [IO.File]::Open($sentinel, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    & (Join-Path $tool 'diagnose-update-access.ps1')
    if (-not $?) { throw 'DIAGNOSTIC_SCRIPT_FAILED' }
    $reportPath = @(Get-ChildItem -LiteralPath $tool -Filter update-access-report.json -Recurse)
    if ($reportPath.Count -ne 1) { throw 'REPORT_MISSING' }
    $report = Get-Content -LiteralPath $reportPath[0].FullName -Raw | ConvertFrom-Json
    if ($report.errors.Count -ne 0) { throw ('UNEXPECTED_ERROR: ' + ($report.errors | ConvertTo-Json -Compress)) }
    if ($report.rename_probe.Count -ne 1 -or $report.rename_probe[0].winerror -ne 0) { throw 'NATIVE_RENAME_NOT_EXERCISED' }
    if ($report.residual_requests.Count -ne 1 -or $report.residual_requests[0].files_checked -ne 1) { throw 'FILE_USER_QUERY_NOT_EXERCISED' }
    if ($report.residual_requests[0].restart_manager_code -ne 0) { throw 'FILE_USER_QUERY_FAILED' }
    if (@($report.residual_requests[0].file_users | Where-Object { $_.pid -eq $PID }).Count -ne 1) { throw 'ACTUAL_LOCK_HOLDER_NOT_RECORDED' }
    if ((Get-Content -LiteralPath $sentinel -Raw) -ne 'unchanged-existing-cache') { throw 'EXISTING_CACHE_CHANGED' }
    if (@(Get-ChildItem -LiteralPath $requests -Directory).Count -ne 1) { throw 'SYNTHETIC_CLEANUP_FAILED' }
    $json = Get-Content -LiteralPath $reportPath[0].FullName -Raw
    if ($json.Contains($root) -or $json.Contains([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)) { throw 'PRIVATE_PATH_OR_SID_LEAK' }
    $code = [CheJinAccessNative]::Rename(('\\?\' + $stage), ('\\?\' + (Join-Path $original 'staging')))
    if ($code -notin @(5,32)) { throw ('HELD_FILE_DID_NOT_BLOCK_RENAME: ' + $code) }
    $held.Dispose(); $held = $null
    if ([CheJinAccessNative]::Rename(('\\?\' + $stage), ('\\?\' + (Join-Path $original 'staging'))) -ne 0) { throw 'UNLOCKED_RENAME_FAILED' }
    Write-Host 'PASS native rename, read-only existing-cache inspection, regular-file Restart Manager, scoped cleanup and report redaction'
} finally {
    if ($held) { $held.Dispose() }
    $env:LOCALAPPDATA = $saved
    # Entire root was created by this isolated test, never a production path.
    Remove-Item -LiteralPath $root -Recurse -Force
}
