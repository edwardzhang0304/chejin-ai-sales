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
    function Invoke-Setup([string]$response) {
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = Join-Path $env:SystemRoot 'System32/cmd.exe'
        $start.Arguments = '/d /c ""' + $entry + '""'
        $start.WorkingDirectory = $root
        $start.UseShellExecute = $false
        $start.RedirectStandardInput = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $process = [Diagnostics.Process]::Start($start)
        $process.StandardInput.WriteLine($response)
        $process.StandardInput.WriteLine('')
        $process.StandardInput.Close()
        $output = $process.StandardOutput.ReadToEnd() + $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return @{ Code = $process.ExitCode; Output = $output }
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
    Write-Host 'PASS Hook one-click setup: missing profile, declined confirmation, successful shortcut setup'
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
