# Runs in Windows PowerShell 5.1; all process operations are simulated.
$ErrorActionPreference = 'Stop'
$names = @('CHEJIN_WORKER_HOME', 'CHEJIN_C2_ENABLED', 'CHEJIN_API_BASE_URL', 'LOCALAPPDATA')
$original = @{}
foreach ($name in $names) { $original[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$root = Join-Path ([IO.Path]::GetTempPath()) ('hook-launcher-test-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $root | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot '../packaging/start-hook.ps1') -Destination $root
New-Item -ItemType File -Path (Join-Path $root 'CheJinWorkerClient.exe') | Out-Null
Set-Content -LiteralPath (Join-Path $root 'profile.json') -Value '{}' -Encoding UTF8
function Assert-True($condition, $message) { if (-not $condition) { throw $message } }
function Get-Process {
    param($Name, $ErrorAction)
    if ($script:case -ne 'launch-new') { return [pscustomobject]@{ Id = 123 } }
}
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, [switch]$Wait, [switch]$PassThru)
    Assert-True ($env:CHEJIN_WORKER_HOME -eq (Join-Path $root 'CheJinWorkerHook')) 'Hook child home missing'
    Assert-True ($env:CHEJIN_C2_ENABLED -eq 'false') 'Hook child OCR override missing'
    if ($ArgumentList -contains '--write-report') {
        $script:checks++
        $index = [Array]::IndexOf($ArgumentList, '--write-report')
        $script:reportPath = $ArgumentList[$index + 1].Trim('"')
        if ($script:case -eq 'check-failure') {
            @{error_code='HOOK_TEST_FAILED'} | ConvertTo-Json | Set-Content -LiteralPath $script:reportPath -Encoding UTF8
            return [pscustomobject]@{ ExitCode = 4 }
        }
        if ($script:case -eq 'setup') {
            Assert-True ($ArgumentList -contains '--hook-configure') 'Setup CLI missing'
            Assert-True ($ArgumentList -contains '--confirm-auto-text-disabled') 'Voice confirmation missing'
        } else {
            Assert-True ($ArgumentList -contains '--hook-launch-info') 'Launch CLI missing'
        }
        @{api_base_url='https://gray.example.test/api'; exe='C:\Test WeChat\WeChat.exe';
          working_directory='C:\Test WeChat'; arguments=@('StartPort=31234','ConnectType=http','RecvType=0')} |
            ConvertTo-Json | Set-Content -LiteralPath $script:reportPath -Encoding UTF8
        return [pscustomobject]@{ ExitCode = 0 }
    }
    Assert-True ($env:CHEJIN_API_BASE_URL -eq 'https://gray.example.test/api') 'Child backend override missing'
    if ($FilePath -eq 'C:\Test WeChat\WeChat.exe') {
        $script:wechatStarts++
        Assert-True ($WorkingDirectory -eq 'C:\Test WeChat') 'Incorrect WeChat working directory'
        Assert-True (($ArgumentList -join ' ') -eq 'StartPort=31234 ConnectType=http RecvType=0') 'Incomplete WeChat arguments'
        return
    }
    $script:workerStarts++
    if ($script:case -eq 'worker-failure') { throw 'SIMULATED_WORKER_FAILURE' }
    if ($script:case -eq 'worker-exit') { return [pscustomobject]@{ ExitCode = 4 } }
    return [pscustomobject]@{ ExitCode = 0 }
}
try {
    $env:LOCALAPPDATA = $root
    $completed = 0
    foreach ($preset in @($true, $false)) {
        foreach ($script:case in @('setup','launch-existing','launch-new','check-failure','worker-failure','worker-exit')) {
            $before = @{}
            foreach ($name in $names[0..2]) {
                $value = if ($preset) { 'prior-' + $name } else { $null }
                [Environment]::SetEnvironmentVariable($name, $value, 'Process')
                $before[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            }
            $script:checks = 0; $script:wechatStarts = 0; $script:workerStarts = 0; $script:reportPath = $null
            $failed = $false
            $LASTEXITCODE = 999
            $returnedExitCode = $null
            try {
                if ($script:case -eq 'setup') {
                    & (Join-Path $root 'start-hook.ps1') -Configuration (Join-Path $root 'profile.json') -ConfirmAutoTextDisabled
                } else { & (Join-Path $root 'start-hook.ps1') }
                $returnedExitCode = $LASTEXITCODE
            } catch {
                $failed = $true
                if ($script:case -notin @('check-failure','worker-failure')) { throw }
            }
            Assert-True ($failed -eq ($script:case -in @('check-failure','worker-failure'))) 'Unexpected failure outcome'
            if (-not $failed) {
                $expectedExitCode = if ($script:case -eq 'worker-exit') { 4 } else { 0 }
                Assert-True ($returnedExitCode -eq $expectedExitCode) 'Worker exit code was not preserved'
            }
            foreach ($name in $names[0..2]) {
                Assert-True ([Environment]::GetEnvironmentVariable($name, 'Process') -ceq $before[$name]) ('Leaked environment: ' + $name)
            }
            Assert-True ($script:checks -eq 1) 'Expected one configuration check'
            $expectedWeChat = if ($script:case -eq 'launch-new') { 1 } else { 0 }
            $expectedWorker = if ($script:case -in @('setup','check-failure')) { 0 } else { 1 }
            Assert-True ($script:wechatStarts -eq $expectedWeChat) 'Unexpected WeChat start'
            Assert-True ($script:workerStarts -eq $expectedWorker) 'Unexpected Worker start'
            Assert-True (-not (Test-Path -LiteralPath $script:reportPath)) 'Report was not cleaned'
            $completed++
            Write-Host ('PASS ' + $script:case + ' preset=' + $preset)
        }
    }
    Assert-True ($completed -eq 12) 'Some launcher cases did not finish'
    $global:LASTEXITCODE = 0
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $original[$name], 'Process') }
    Remove-Item -LiteralPath $root -Recurse -Force
}
