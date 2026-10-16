param([Parameter(Mandatory=$true)][string]$BinaryDirectory)
$ErrorActionPreference = 'Stop'
$root = Join-Path $env:RUNNER_TEMP ('update-trace-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root | Out-Null
$exe = Join-Path $BinaryDirectory 'CheJinUpdateTrace.exe'
[Reflection.Assembly]::LoadFrom((Join-Path $BinaryDirectory 'Microsoft.Diagnostics.Tracing.TraceEvent.dll')) | Out-Null
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public static class RenameOracle {
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool MoveFileExW(string a,string b,uint flags);
 public static int Rename(string a,string b) { return MoveFileExW(a,b,1)?0:Marshal.GetLastWin32Error(); }
}
'@
function Wait-File([string]$path) {
    $until = (Get-Date).AddSeconds(20)
    while (-not (Test-Path -LiteralPath $path)) {
        if ((Get-Date) -gt $until) { throw ('DIAGNOSTIC_SIGNAL_TIMEOUT: ' + [IO.Path]::GetFileName($path)) }
        Start-Sleep -Milliseconds 100
    }
}
$held = $null; $process = $null; $unrelated = $null
try {
    # A separate existing ETW user session must survive all collector cleanup.
    $otherName = 'CheJinUnrelatedTest-' + [guid]::NewGuid().ToString()
    $unrelated = New-Object Microsoft.Diagnostics.Tracing.Session.TraceEventSession($otherName)
    [void]$unrelated.EnableProvider('Microsoft-Windows-DotNETRuntime')
    foreach ($mode in @('captured-denial-completion','empty-download-timeout','forced-collector-exit')) {
        $base = Join-Path $root $mode
        $cache = Join-Path $base 'CheJinWorkerUpdate'; $requests = Join-Path $cache 'requests'; $output = Join-Path $base 'output'
        New-Item -ItemType Directory -Path $requests,$output -Force | Out-Null
        $seconds = if ($mode -eq 'captured-denial-completion') { 20 } else { 3 }
        $process = Start-Process -FilePath $exe -ArgumentList ('--collect "' + $cache + '" "' + $output + '" ' + $seconds + ' 10') -PassThru
        Wait-File (Join-Path $output 'armed.json')
        $request = Join-Path $requests ('update-' + [guid]::NewGuid().ToString())
        $download = Join-Path $request 'download'; New-Item -ItemType Directory -Path $download -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $download 'client-update.zip.partial'), 'synthetic trigger')
        # Act immediately after ARMED, not after a later readiness signal.
        $capture = Get-Content -LiteralPath (Join-Path $output 'armed.json') -Raw | ConvertFrom-Json
        if ($mode -eq 'forced-collector-exit') {
            $process.Kill(); [void]$process.WaitForExit(10000)
        } elseif ($mode -eq 'empty-download-timeout') {
            if (-not $process.WaitForExit(20000) -or $process.ExitCode -ne 0) { throw 'TIMEOUT_CASE_DID_NOT_EXIT' }
            $summary = Get-Content -LiteralPath (Join-Path $output 'summary.json') -Raw | ConvertFrom-Json
            if ($summary.reason -ne 'capture_time_limit_evidence_incomplete' -or $summary.own_session_remaining) { throw 'TIMEOUT_REASON_OR_CLEANUP_FAILED' }
            Write-Host 'PASS distinct no-staging capture timeout and session cleanup'
        } else {
            $stage = Join-Path $request 'staging.extracting'; $target = Join-Path $request 'staging'
            New-Item -ItemType Directory -Path $stage | Out-Null
            $member = Join-Path $stage 'sentinel.txt'; [IO.File]::WriteAllText($member, 'synthetic only')
            $held = [IO.File]::Open($member,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite)
            $denied = [RenameOracle]::Rename($stage,$target)
            if ($denied -notin @(5,32)) { throw ('NATIVE_DENIAL_NOT_REPRODUCED: ' + $denied) }
            # The denied operation above happens immediately after ARMED.
            # Now keep staging alive so the distinct completion monitor observes it.
            Wait-File (Join-Path $output 'capturing.json')
            Start-Sleep -Milliseconds 1200
            [IO.File]::WriteAllText((Join-Path $base 'NEVER_EXPORT_PRIVATE_CANARY.txt'), 'never part of scope')
            $held.Dispose(); $held = $null
            if ([RenameOracle]::Rename($stage,$target) -ne 0) { throw 'RELEASED_RENAME_FAILED' }
            if (-not $process.WaitForExit(20000)) { throw 'CAPTURE_LIMIT_DID_NOT_STOP' }
            if ($process.ExitCode -ne 0) { throw 'COLLECTOR_FAILED' }
            $summary = Get-Content -LiteralPath (Join-Path $output 'summary.json') -Raw | ConvertFrom-Json
            if ($summary.reason -ne 'staging_promotion_observed_not_upgrade_acceptance' -or $summary.own_session_remaining) { throw 'PHASE_COMPLETION_OR_CLEANUP_FAILED' }
            $events = @(Get-Content -LiteralPath (Join-Path $output 'cache-events.jsonl') | ForEach-Object { $_ | ConvertFrom-Json })
            if (-not @($events | Where-Object { $_.operation -eq 'Create' -and $_.path.EndsWith('sentinel.txt') -and ($_.detail.share_access -band 4) -eq 0 }).Count) { throw 'OPEN_SHARE_MODE_NOT_CAPTURED' }
            $failed = @($events | Where-Object { $_.operation -eq 'OperationEnd' -and $_.ntstatus -in @('C0000022','C0000043') -and
                $_.path.EndsWith('staging.extracting') -and $_.pid -eq $PID -and $_.for_operation -in @('Create','Rename','SetInfo') })
            if (-not $failed.Count) { throw 'ACTUAL_RENAME_DENIAL_STATUS_NOT_CAPTURED' }
            foreach ($end in $failed) {
                $start = @($events | Where-Object { $_.sequence -eq $end.sequence -and $_.operation -eq $end.for_operation -and $_.path -eq $end.path })
                if ($start.Count -ne 1) { throw 'DENIAL_OPERATION_CORRELATION_AMBIGUOUS' }
            }
            if (-not @($events | Where-Object { $_.operation -eq 'OperationEnd' -and $_.ntstatus -eq '00000000' -and $_.pid -eq $PID -and
                $_.for_operation -in @('Rename','SetInfo') -and $_.path.EndsWith('staging.extracting') }).Count) { throw 'UNLOCKED_RENAME_SUCCESS_NOT_CAPTURED' }
            foreach ($f in Get-ChildItem -LiteralPath $output -File) {
                $text = Get-Content -LiteralPath $f.FullName -Raw
                if ($text.Contains('NEVER_EXPORT_PRIVATE_CANARY') -or $text.Contains($root.Replace('\','\\'))) { throw 'OUTSIDE_SCOPE_OR_ABSOLUTE_PATH_EXPORTED' }
            }
            Write-Host ('PASS actual lock share mode, denied native rename WinError ' + $denied + ', correlated failure status, output filtering and bounded stop')
        }
        Wait-File (Join-Path $output 'watchdog.json')
        $watchdog = Get-Content -LiteralPath (Join-Path $output 'watchdog.json') -Raw | ConvertFrom-Json
        if (-not $watchdog.owned_session_stopped -or [Microsoft.Diagnostics.Tracing.Session.TraceEventSession]::GetActiveSessionNames().Contains($capture.session)) { throw 'WATCHDOG_CLEANUP_FAILED' }
        if (-not [Microsoft.Diagnostics.Tracing.Session.TraceEventSession]::GetActiveSessionNames().Contains($otherName)) { throw 'UNRELATED_SESSION_STOPPED' }
        Write-Host ('PASS watchdog owns only its session: ' + $mode)
        $process.Dispose(); $process = $null
    }
} finally {
    if ($held) { $held.Dispose() }
    if ($process -and -not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(10000) }
    if ($unrelated) { $unrelated.Dispose() }
    # Keep synthetic output on the runner for failure evidence, never real customer data.
    Write-Host ('Synthetic evidence: ' + $root)
}
exit 0
