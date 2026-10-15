# Runs in Windows PowerShell 5.1; all process operations are simulated.
$ErrorActionPreference = 'Stop'
$names = @('CHEJIN_WORKER_HOME', 'CHEJIN_C2_ENABLED', 'CHEJIN_API_BASE_URL', 'LOCALAPPDATA')
$original = @{}
foreach ($name in $names) { $original[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$root = Join-Path ([IO.Path]::GetTempPath()) ('hook-launcher-test-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $root | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot '../packaging/start-hook.ps1') -Destination $root
New-Item -ItemType File -Path (Join-Path $root 'CheJinWorkerClient.exe') | Out-Null
$global:HookTestWeChatDir = Join-Path $root 'Test WeChat'
New-Item -ItemType Directory -Path $global:HookTestWeChatDir | Out-Null
$global:HookTestWeChatPath = Join-Path $global:HookTestWeChatDir 'WeChat.exe'
New-Item -ItemType File -Path $global:HookTestWeChatPath | Out-Null
Set-Content -LiteralPath (Join-Path $root 'profile.json') -Value '{}' -Encoding UTF8
function Assert-True($condition, $message) { if (-not $condition) { throw $message } }
function Get-Process {
    param($Name, $ErrorAction)
    if ($global:HookLauncherCase -ne 'launch-new') { return [pscustomobject]@{ Id = 123 } }
}
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, [switch]$Wait, [switch]$PassThru)
    Assert-True ($env:CHEJIN_WORKER_HOME -eq (Join-Path $root 'CheJinWorkerHook')) 'Hook child home missing'
    Assert-True ($env:CHEJIN_C2_ENABLED -eq 'false') 'Hook child OCR override missing'
    if ($ArgumentList -contains '--write-report') {
        $global:HookLauncherChecks++
        $index = [Array]::IndexOf($ArgumentList, '--write-report')
        $global:HookLauncherReportPath = $ArgumentList[$index + 1].Trim('"')
        if ($global:HookLauncherCase -eq 'check-failure') {
            @{error_code='HOOK_TEST_FAILED'} | ConvertTo-Json | Set-Content -LiteralPath $global:HookLauncherReportPath -Encoding UTF8
            return [pscustomobject]@{ ExitCode = 4 }
        }
        if ($global:HookLauncherCase -eq 'setup') {
            Assert-True ($ArgumentList -contains '--hook-configure') 'Setup CLI missing'
            Assert-True ($ArgumentList -contains '--confirm-auto-text-disabled') 'Voice confirmation missing'
        } else {
            Assert-True ($ArgumentList -contains '--hook-launch-info') 'Launch CLI missing'
        }
        @{api_base_url='https://gray.example.test/api'; exe=$global:HookTestWeChatPath;
          working_directory=$global:HookTestWeChatDir; arguments=@('StartPort=31234','ConnectType=http','RecvType=0')} |
            ConvertTo-Json | Set-Content -LiteralPath $global:HookLauncherReportPath -Encoding UTF8
        return [pscustomobject]@{ ExitCode = 0 }
    }
    Assert-True ($env:CHEJIN_API_BASE_URL -eq 'https://gray.example.test/api') 'Child backend override missing'
    if ($FilePath -eq $global:HookTestWeChatPath) {
        $global:HookLauncherWeChatStarts++
        Assert-True ($WorkingDirectory -eq $global:HookTestWeChatDir) 'Incorrect WeChat working directory'
        Assert-True (($ArgumentList -join ' ') -eq 'StartPort=31234 ConnectType=http RecvType=0') 'Incomplete WeChat arguments'
        return
    }
    $global:HookLauncherWorkerStarts++
    if ($global:HookLauncherCase -eq 'worker-failure') { throw 'SIMULATED_WORKER_FAILURE' }
    if ($global:HookLauncherCase -eq 'worker-exit') { return [pscustomobject]@{ ExitCode = 4 } }
    return [pscustomobject]@{ ExitCode = 0 }
}
try {
    $env:LOCALAPPDATA = $root
    $completed = 0
    foreach ($preset in @($true, $false)) {
        foreach ($global:HookLauncherCase in @('setup','launch-existing','launch-new','check-failure','worker-failure','worker-exit')) {
            $before = @{}
            foreach ($name in $names[0..2]) {
                $value = if ($preset) { 'prior-' + $name } else { $null }
                [Environment]::SetEnvironmentVariable($name, $value, 'Process')
                $before[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            }
            $global:HookLauncherChecks = 0; $global:HookLauncherWeChatStarts = 0; $global:HookLauncherWorkerStarts = 0; $global:HookLauncherReportPath = $null
            $failed = $false
            $global:LASTEXITCODE = 999
            $returnedExitCode = $null
            try {
                if ($global:HookLauncherCase -eq 'setup') {
                    & (Join-Path $root 'start-hook.ps1') -Configuration (Join-Path $root 'profile.json') -ConfirmAutoTextDisabled
                } else { & (Join-Path $root 'start-hook.ps1') }
                $returnedExitCode = $global:LASTEXITCODE
            } catch {
                $failed = $true
                if ($global:HookLauncherCase -notin @('check-failure','worker-failure')) { throw }
            }
            Assert-True ($failed -eq ($global:HookLauncherCase -in @('check-failure','worker-failure'))) 'Unexpected failure outcome'
            if (-not $failed) {
                $expectedExitCode = if ($global:HookLauncherCase -eq 'worker-exit') { 4 } else { 0 }
                Assert-True ($returnedExitCode -eq $expectedExitCode) ('Worker exit code was not preserved in ' + $global:HookLauncherCase + ': expected ' + $expectedExitCode + ', got ' + $returnedExitCode)
            }
            foreach ($name in $names[0..2]) {
                Assert-True ([Environment]::GetEnvironmentVariable($name, 'Process') -ceq $before[$name]) ('Leaked environment: ' + $name)
            }
            Assert-True ($global:HookLauncherChecks -eq 1) 'Expected one configuration check'
            $expectedWeChat = if ($global:HookLauncherCase -eq 'launch-new') { 1 } else { 0 }
            $expectedWorker = if ($global:HookLauncherCase -in @('setup','check-failure')) { 0 } else { 1 }
            Assert-True ($global:HookLauncherWeChatStarts -eq $expectedWeChat) 'Unexpected WeChat start'
            Assert-True ($global:HookLauncherWorkerStarts -eq $expectedWorker) 'Unexpected Worker start'
            Assert-True (-not (Test-Path -LiteralPath $global:HookLauncherReportPath)) 'Report was not cleaned'
            $completed++
            Write-Host ('PASS ' + $global:HookLauncherCase + ' preset=' + $preset)
        }
    }
    Assert-True ($completed -eq 12) 'Some launcher cases did not finish'
    $desktop = [Environment]::GetFolderPath('Desktop')
    $wechatLink = Join-Path $desktop '工作微信.lnk'
    $workerLink = Join-Path $desktop '车金 Worker.lnk'
    Assert-True (-not (Test-Path -LiteralPath $wechatLink) -and -not (Test-Path -LiteralPath $workerLink)) 'Test desktop shortcuts already exist'
    try {
        $global:HookLauncherCase = 'setup'
        & (Join-Path $root 'start-hook.ps1') -Configuration (Join-Path $root 'profile.json') -ConfirmAutoTextDisabled -CreateShortcuts
        $shell = New-Object -ComObject WScript.Shell
        Assert-True ((Test-Path -LiteralPath $wechatLink) -and (Test-Path -LiteralPath $workerLink)) 'First-run shortcuts missing'
        $wechat = $shell.CreateShortcut($wechatLink)
        $worker = $shell.CreateShortcut($workerLink)
        Assert-True ($wechat.TargetPath -eq $global:HookTestWeChatPath) 'WeChat shortcut target mismatch'
        Assert-True ($wechat.WorkingDirectory -eq $global:HookTestWeChatDir) 'WeChat shortcut working directory mismatch'
        Assert-True ($wechat.Arguments -eq 'StartPort=31234 ConnectType=http RecvType=0') 'WeChat shortcut parameters missing'
        Assert-True ($worker.TargetPath -eq (Join-Path $PSHOME 'powershell.exe')) 'Worker shortcut target mismatch'
        Assert-True ($worker.Arguments -like '*start-hook.ps1*') 'Worker shortcut must use the checked launcher'
        Write-Host 'PASS Hook desktop shortcuts use the saved launch configuration'
    } finally {
        Remove-Item -LiteralPath $wechatLink, $workerLink -Force -ErrorAction SilentlyContinue
    }
    $global:LASTEXITCODE = 0
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $original[$name], 'Process') }
    Remove-Item -LiteralPath $root -Recurse -Force
    Remove-Variable -Scope Global -Name HookLauncherCase,HookLauncherChecks,HookLauncherReportPath,HookLauncherWeChatStarts,HookLauncherWorkerStarts,HookTestWeChatDir,HookTestWeChatPath -ErrorAction SilentlyContinue
}
