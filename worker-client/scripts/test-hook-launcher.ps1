# Process starts are simulated; directory rename uses the real Windows filesystem.
$ErrorActionPreference = 'Stop'
$originalLocation = Get-Location
$originalNativeDirectory = [Environment]::CurrentDirectory
$names = @('CHEJIN_WORKER_HOME', 'CHEJIN_C2_ENABLED', 'CHEJIN_API_BASE_URL', 'LOCALAPPDATA')
$original = @{}
foreach ($name in $names) { $original[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$root = Join-Path ([IO.Path]::GetTempPath()) ('hook-launcher-test-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $root | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot '../packaging/start-hook.ps1') -Destination (Join-Path $root 'start-hook.ps1')
New-Item -ItemType File -Path (Join-Path $root 'CheJinWorkerClient.exe') | Out-Null
$global:HookTestWeChatDir = Join-Path $root 'Test WeChat'
New-Item -ItemType Directory -Path $global:HookTestWeChatDir | Out-Null
$global:HookTestWeChatPath = Join-Path $global:HookTestWeChatDir 'WeChat.exe'
Copy-Item -LiteralPath (Join-Path $env:SystemRoot 'System32/notepad.exe') -Destination $global:HookTestWeChatPath
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
    Assert-True ($WorkingDirectory -eq $root) 'Worker must retain its own working directory'
    # The waiting launcher remains alive here, just as during button updating.
    # A native CWD under $root makes this actual rename fail with WinError 32.
    $renamedRoot = $root + '.rename-probe'
    try {
        [IO.Directory]::Move($root, $renamedRoot)
        [IO.Directory]::Move($renamedRoot, $root)
    } catch {
        $cause = $_.Exception
        while ($cause.InnerException) { $cause = $cause.InnerException }
        if (($cause.HResult -band 0xFFFF) -eq 32) { throw 'HOOK_LAUNCHER_PROGRAM_DIRECTORY_LOCKED' }
        throw
    }
    if ($global:HookLauncherCase -eq 'worker-failure') { throw 'SIMULATED_WORKER_FAILURE' }
    if ($global:HookLauncherCase -eq 'worker-exit') { return [pscustomobject]@{ ExitCode = 4 } }
    return [pscustomobject]@{ ExitCode = 0 }
}
try {
    $env:LOCALAPPDATA = $root
    # Stable negative control; do not depend on HEAD^ still containing the bug.
    [Environment]::CurrentDirectory = $root
    $locked = $false
    try { [IO.Directory]::Move($root, $root + '.locked-probe') } catch {
        $cause = $_.Exception
        while ($cause.InnerException) { $cause = $cause.InnerException }
        if (($cause.HResult -band 0xFFFF) -ne 32) { throw }
        $locked = $true
    } finally { [Environment]::CurrentDirectory = $originalNativeDirectory }
    Assert-True $locked 'Windows native current directory lock was not reproduced'
    Write-Host 'PASS native program directory lock reproduced'
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
            Set-Location -LiteralPath $root
            [Environment]::CurrentDirectory = $root
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
    New-Item -ItemType Directory -Path $desktop -Force | Out-Null
    $wechatLink = Join-Path $desktop 'Work WeChat.lnk'
    $workerLink = Join-Path $desktop 'CheJin Worker.lnk'
    Assert-True (-not (Test-Path -LiteralPath $wechatLink) -and -not (Test-Path -LiteralPath $workerLink)) 'Test desktop shortcuts already exist'
    $controlLink = Join-Path $desktop 'hook-control.lnk'
    try {
        $control = (New-Object -ComObject WScript.Shell).CreateShortcut($controlLink)
        $control.TargetPath = $global:HookTestWeChatPath
        $control.WorkingDirectory = $global:HookTestWeChatDir
        $control.Save()
        Assert-True (Test-Path -LiteralPath $controlLink) 'Windows shortcut control could not be saved'
        $global:HookLauncherCase = 'setup'
        $global:HookLauncherWorkerStarts = 0; $global:HookLauncherWeChatStarts = 0
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
        Assert-True ($worker.Arguments -like '*-WindowStyle Hidden*') 'Worker shortcut must hide the persistent console'
        Assert-True ($worker.Arguments -like '*-ShowErrorDialog*') 'Hidden launcher must retain visible failures'
        Assert-True ($worker.WorkingDirectory.TrimEnd('\') -ieq ([IO.Path]::GetTempPath()).TrimEnd('\')) 'Worker shortcut must start outside the program directory'
        Assert-True ($global:HookLauncherWorkerStarts -eq 0 -and $global:HookLauncherWeChatStarts -eq 0) 'Setup must not start work'
        # Refresh an existing shortcut without a profile or repeated configuration.
        $worker.WorkingDirectory = $root
        $worker.Save()
        Remove-Item -LiteralPath (Join-Path $root 'profile.json') -Force
        $global:HookLauncherCase = 'launch-existing'
        & (Join-Path $root 'start-hook.ps1') -CreateShortcuts
        $refreshed = $shell.CreateShortcut($workerLink)
        Assert-True ($refreshed.WorkingDirectory.TrimEnd('\') -ieq ([IO.Path]::GetTempPath()).TrimEnd('\')) 'Existing shortcut was not refreshed'
        Assert-True ($global:HookLauncherWorkerStarts -eq 0 -and $global:HookLauncherWeChatStarts -eq 0) 'Refresh must not launch Worker or WeChat'
        Write-Host 'PASS existing shortcut refresh uses saved config without starting processes'
        Write-Host 'PASS Hook desktop shortcuts use the saved launch configuration'
    } finally {
        Remove-Item -LiteralPath $controlLink, $wechatLink, $workerLink -Force -ErrorAction SilentlyContinue
    }
    $global:LASTEXITCODE = 0
} finally {
    Set-Location -LiteralPath $originalLocation.Path
    [Environment]::CurrentDirectory = $originalNativeDirectory
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $original[$name], 'Process') }
    Remove-Item -LiteralPath $root -Recurse -Force
    Remove-Variable -Scope Global -Name HookLauncherCase,HookLauncherChecks,HookLauncherReportPath,HookLauncherWeChatStarts,HookLauncherWorkerStarts,HookTestWeChatDir,HookTestWeChatPath -ErrorAction SilentlyContinue
}
