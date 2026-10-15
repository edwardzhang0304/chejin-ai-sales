"""Run the real launcher with process/Windows dialog doubles; never start WeChat."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / 'packaging/start-hook.ps1'


@pytest.mark.parametrize('case,hidden,expected_exit,dialog', [
    ('existing', True, 0, None),
    ('new', True, 0, None),
    ('check-failure', True, 1, 'HOOK_TEST_FAILED'),
    ('worker-failure', True, 1, 'HOOK_LAUNCH_FAILED'),
    ('missing-exe', True, 1, 'HOOK_WORKER_EXECUTABLE_MISSING'),
    ('worker-exit', True, 4, 'HOOK_WORKER_EXIT_4'),
    ('check-failure', False, None, None),
    ('worker-exit', False, 4, None),
])
def test_real_launcher_hidden_and_diagnostic_outcomes(tmp_path, case, hidden, expected_exit, dialog):
    powershell = os.environ.get('CHEJIN_TEST_POWERSHELL') or shutil.which('powershell.exe') or shutil.which('pwsh')
    if not powershell:
        pytest.skip('PowerShell runtime required')
    shutil.copyfile(LAUNCHER, tmp_path / 'start-hook.ps1')
    if case != 'missing-exe':
        (tmp_path / 'CheJinWorkerClient.exe').touch()
    runner = tmp_path / 'test.ps1'
    runner.write_text(r'''param([string]$Case, [string]$Hidden)
$ErrorActionPreference = 'Stop'
$global:LaunchTestStarts = @()
$global:LaunchTestDialogs = @()
$global:LaunchTestReport = $null
$env:LOCALAPPDATA = $PSScriptRoot
$names = @('CHEJIN_WORKER_HOME','CHEJIN_C2_ENABLED','CHEJIN_API_BASE_URL')
foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, 'prior-' + $name, 'Process') }
function New-Object {
    param($ComObject)
    if ($ComObject -ne 'WScript.Shell') { throw 'Unexpected COM object' }
    $shell = [pscustomobject]@{}
    $shell | Add-Member -MemberType ScriptMethod -Name Popup -Value {
        param($message, $timeout, $title, $icon)
        $global:LaunchTestDialogs += $message
        return 1
    }
    return $shell
}
function Get-Process {
    param($Name, $ErrorAction)
    if ($Case -ne 'new') { return [pscustomobject]@{Id=123} }
}
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, [switch]$Wait, [switch]$PassThru)
    if ($env:CHEJIN_C2_ENABLED -ne 'false' -or $env:CHEJIN_WORKER_HOME -ne (Join-Path $PSScriptRoot 'CheJinWorkerHook')) { throw 'Child environment changed' }
    if ($ArgumentList -contains '--write-report') {
        $global:LaunchTestStarts += 'check'
        $global:LaunchTestReport = $ArgumentList[[Array]::IndexOf($ArgumentList, '--write-report') + 1].Trim('"')
        if ($Case -eq 'check-failure') {
            @{error_code='HOOK_TEST_FAILED'} | ConvertTo-Json | Set-Content $global:LaunchTestReport
            return [pscustomobject]@{ExitCode=4}
        }
        @{exe='test-wechat.exe'; working_directory='test-wechat-dir'; api_base_url='https://gray.example.test/api';
          arguments=@('StartPort=31234','ConnectType=http','RecvType=0')} | ConvertTo-Json | Set-Content $global:LaunchTestReport
        return [pscustomobject]@{ExitCode=0}
    }
    if ($env:CHEJIN_API_BASE_URL -ne 'https://gray.example.test/api') { throw 'Backend configuration changed' }
    if ($FilePath -eq 'test-wechat.exe') {
        if (($ArgumentList -join ' ') -ne 'StartPort=31234 ConnectType=http RecvType=0' -or $WorkingDirectory -ne 'test-wechat-dir') { throw 'WeChat parameters changed' }
        $global:LaunchTestStarts += 'wechat'
        return
    }
    $global:LaunchTestStarts += 'worker'
    if ($Case -eq 'worker-failure') { throw 'SIMULATED_FAILURE' }
    $code = if ($Case -eq 'worker-exit') { 4 } else { 0 }
    return [pscustomobject]@{ExitCode=$code}
}
$caught = $false
$code = $null
try {
    & (Join-Path $PSScriptRoot 'start-hook.ps1') -ShowErrorDialog:($Hidden -eq 'true')
    $code = $LASTEXITCODE
} catch { $caught = $true }
foreach ($name in $names) {
    if ([Environment]::GetEnvironmentVariable($name, 'Process') -ne ('prior-' + $name)) { throw 'Environment not restored' }
}
if ($global:LaunchTestReport -and (Test-Path $global:LaunchTestReport)) { throw 'Temporary report leaked' }
@{exit_code=$code; caught=$caught; dialogs=@($global:LaunchTestDialogs); starts=@($global:LaunchTestStarts)} |
    ConvertTo-Json -Compress | Set-Content (Join-Path $PSScriptRoot 'result.json')
exit 0
''', encoding='utf-8-sig')
    result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-File', str(runner), case, str(hidden).lower()],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    outcome = json.loads((tmp_path / 'result.json').read_text(encoding='utf-8-sig'))
    assert outcome['exit_code'] == expected_exit
    assert outcome['caught'] is (expected_exit is None)
    assert len(outcome['dialogs']) == (1 if dialog else 0)
    if dialog:
        assert dialog in outcome['dialogs'][0]
        assert 'SIMULATED_FAILURE' not in outcome['dialogs'][0]
    expected_starts = [] if case == 'missing-exe' else ['check']
    if case == 'new':
        expected_starts.append('wechat')
    if case not in ('missing-exe', 'check-failure'):
        expected_starts.append('worker')
    assert outcome['starts'] == expected_starts
