# Windows-native check of the double-click first-run entry. The Worker process is stubbed.
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('hook-setup-test-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $root | Out-Null
try {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '../packaging/Setup-CheJin-Hook.cmd') -Destination $root
    $stub = @'
param([string]$Configuration, [switch]$ConfirmAutoTextDisabled, [switch]$CreateShortcuts)
if ($Configuration -ne (Join-Path $PSScriptRoot 'profile.json') -or
    -not $ConfirmAutoTextDisabled -or -not $CreateShortcuts) { exit 7 }
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'called.txt'), 'setup-called')
exit 0
'@
    Set-Content -LiteralPath (Join-Path $root 'start-hook.ps1') -Value $stub -Encoding UTF8
    $entry = Join-Path $root 'Setup-CheJin-Hook.cmd'
    $marker = Join-Path $root 'called.txt'
    function Start-Setup {
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = Join-Path $env:SystemRoot 'System32/cmd.exe'
        $start.Arguments = '/d /c ""' + $entry + '""'
        $start.WorkingDirectory = $root
        $start.UseShellExecute = $false
        $start.RedirectStandardInput = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        return [Diagnostics.Process]::Start($start)
    }
    function Invoke-Setup([string]$response) {
        $process = Start-Setup
        $process.StandardInput.WriteLine($response)
        $process.StandardInput.WriteLine('')
        $process.StandardInput.Close()
        $output = $process.StandardOutput.ReadToEnd() + $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return @{ Code = $process.ExitCode; Output = $output }
    }
    function Assert-PausedDirectoryFree([string]$OutputMarker, [switch]$Confirm) {
        $process = Start-Setup
        try {
            if ($Confirm) { $process.StandardInput.WriteLine('Y') }
            $seen = $false
            for ($i = 0; $i -lt 20; $i++) {
                $line = $process.StandardOutput.ReadLineAsync()
                if (-not $line.Wait(15000) -or $null -eq $line.Result) { throw 'Entry did not reach success pause' }
                if ($line.Result.Contains($OutputMarker)) { $seen = $true; break }
            }
            if (-not $seen -or $process.WaitForExit(200)) { throw 'Entry was not held open at pause' }
            [IO.Directory]::Move($root, $root + '.paused-probe')
            [IO.Directory]::Move($root + '.paused-probe', $root)
            $process.StandardInput.WriteLine('')
            $process.StandardInput.Close()
            if (-not $process.WaitForExit(10000) -or $process.ExitCode -ne 0) { throw 'Entry failed after directory handoff' }
            Write-Host ('PASS live CMD pause does not lock program directory: ' + $OutputMarker)
        } finally {
            if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
            $process.Dispose()
        }
    }
    $missing = Invoke-Setup ''
    if ($missing.Code -ne 2 -or (Test-Path -LiteralPath $marker)) { throw 'Missing profile did not fail closed' }
    Set-Content -LiteralPath (Join-Path $root 'profile.json') -Value '{}' -Encoding UTF8
    $declined = Invoke-Setup 'N'
    if ($declined.Code -ne 2 -or (Test-Path -LiteralPath $marker)) { throw 'Declined voice setting did not stop setup' }
    $accepted = Invoke-Setup 'Y'
    if ($accepted.Code -ne 0 -or -not (Test-Path -LiteralPath $marker)) {
        throw ('One-click setup did not invoke the checked launcher: ' + $accepted.Output)
    }
    Assert-PausedDirectoryFree -OutputMarker 'Setup completed.' -Confirm
    Write-Host 'PASS Hook one-click setup: missing profile, declined confirmation, successful shortcut setup'
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '../packaging/Refresh-CheJin-Shortcuts.cmd') -Destination $root
    $refreshStub = @'
param([string]$Configuration, [switch]$CreateShortcuts, [switch]$ShowErrorDialog)
if ($Configuration -or -not $CreateShortcuts -or -not $ShowErrorDialog) { exit 8 }
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'called.txt'), 'refresh-only')
exit 0
'@
    Set-Content -LiteralPath (Join-Path $root 'start-hook.ps1') -Value $refreshStub -Encoding UTF8
    Remove-Item -LiteralPath (Join-Path $root 'profile.json'), $marker -Force
    $entry = Join-Path $root 'Refresh-CheJin-Shortcuts.cmd'
    $refreshed = Invoke-Setup ''
    if ($refreshed.Code -ne 0 -or (Get-Content -LiteralPath $marker -Raw) -ne 'refresh-only') { throw 'Double-click refresh entry did not preserve refresh-only mode' }
    # Refresh exits after success rather than leaving another launcher window.
    # Invoke-Setup above already waited for this real CMD to exit normally.
    [IO.Directory]::Move($root, $root + '.refresh-completed-probe')
    [IO.Directory]::Move($root + '.refresh-completed-probe', $root)
    Write-Host 'PASS shortcut refresh exits and releases the program directory'
    Set-Content -LiteralPath (Join-Path $root 'start-hook.ps1') -Value 'exit 9' -Encoding UTF8
    $failedRefresh = Invoke-Setup ''
    if ($failedRefresh.Code -ne 9) { throw 'Double-click shortcut refresh hid the failure exit code' }
    Write-Host 'PASS double-click shortcut refresh needs no profile or command'
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
