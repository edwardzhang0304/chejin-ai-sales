param(
  [string]$Configuration = "",
  [switch]$ConfirmAutoTextDisabled,
  [switch]$CreateShortcuts
)
$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'CheJinWorkerClient.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Hook Worker executable missing' }
if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA missing' }
$report = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString() + '.json')
$savedEnvironment = @{}
foreach ($name in @('CHEJIN_WORKER_HOME', 'CHEJIN_C2_ENABLED', 'CHEJIN_API_BASE_URL')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $env:CHEJIN_WORKER_HOME = Join-Path $env:LOCALAPPDATA 'CheJinWorkerHook'
    $env:CHEJIN_C2_ENABLED = 'false'
    $arguments = @('--hook-launch-info', '--write-report', ('"' + $report + '"'))
    if ($Configuration) {
        if (-not $ConfirmAutoTextDisabled) { throw 'Confirm automatic voice transcription is OFF before setup' }
        $resolved = (Resolve-Path -LiteralPath $Configuration).Path
        $arguments = @('--hook-configure', ('"' + $resolved + '"'), '--confirm-auto-text-disabled', '--write-report', ('"' + $report + '"'))
    }
    $check = Start-Process -FilePath $exe -ArgumentList $arguments -Wait -PassThru
    if ($check.ExitCode -ne 0) {
        $errorCode = 'HOOK_INSTALL_FAILED'
        if (Test-Path -LiteralPath $report) {
            $failure = Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($failure.error_code -match '^HOOK_[A-Z0-9_]+$') { $errorCode = $failure.error_code }
        }
        throw "$errorCode (exit $($check.ExitCode)); no WeChat started"
    }
    $launch = Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($CreateShortcuts) {
        $shell = New-Object -ComObject WScript.Shell
        $desktop = [Environment]::GetFolderPath('Desktop')
        $wechatShortcut = $shell.CreateShortcut((Join-Path $desktop '工作微信.lnk'))
        $wechatShortcut.TargetPath = $launch.exe
        $wechatShortcut.WorkingDirectory = $launch.working_directory
        $wechatShortcut.Arguments = $launch.arguments -join ' '
        $wechatShortcut.Save()
        $workerShortcut = $shell.CreateShortcut((Join-Path $desktop '车金 Worker.lnk'))
        $workerShortcut.TargetPath = Join-Path $PSHOME 'powershell.exe'
        $workerShortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"'
        $workerShortcut.WorkingDirectory = $PSScriptRoot
        $workerShortcut.Save()
    }
    if ($Configuration) {
        Write-Host 'Setup complete. Pair this PC with its own Worker using the normal UI. No work has started.'
        exit 0
    }
    $env:CHEJIN_API_BASE_URL = $launch.api_base_url
    if (-not (Get-Process -Name WeChat,Weixin -ErrorAction SilentlyContinue)) {
        Start-Process -FilePath $launch.exe -WorkingDirectory $launch.working_directory -ArgumentList $launch.arguments
    }
    Write-Host 'Keep work WeChat open. Worker verifies the existing process; Start accepting remains manual.'
    $worker = Start-Process -FilePath $exe -WorkingDirectory $PSScriptRoot -Wait -PassThru
    exit $worker.ExitCode
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    if (Test-Path -LiteralPath $report) { Remove-Item -LiteralPath $report -Force }
}
