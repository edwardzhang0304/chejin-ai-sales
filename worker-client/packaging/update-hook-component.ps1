param([ValidateSet('Update','Rollback')][string]$Mode='Update')
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
$caption='车金 Hook 组件维护'
$exe=Join-Path $PSScriptRoot 'CheJinWorkerClient.exe'
$exitCode=1
try {
    if (-not (Test-Path -LiteralPath $exe)) { throw '缺少配套 Worker，请使用完整客户端目录中的入口。' }
    if (-not $env:LOCALAPPDATA) { throw '无法确定本机数据目录。' }
    $message='请先暂停接单，关闭 Worker，并从托盘退出工作微信。原账号、激活信息、聊天记录和发送凭证会保留。'
    if ($Mode -eq 'Rollback') { $message += "`r`n本次将恢复组件更新前的 DLL 和配置。" }
    else { $message += "`r`n本次将更新提供方的 Hook DLL；更新后仍需验证语音和文字功能。" }
    $message += "`r`n请确认微信自动语音转文字仍为关闭状态。是否继续？"
    if ([Windows.Forms.MessageBox]::Show($message,$caption,'OKCancel','Information') -ne 'OK') { exit 0 }
    $env:CHEJIN_WORKER_HOME=Join-Path $env:LOCALAPPDATA 'CheJinWorkerHook'
    $env:CHEJIN_C2_ENABLED='false'
    $reportDir=Join-Path $env:CHEJIN_WORKER_HOME 'artifacts\hook\component-updates'
    New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
    $report=Join-Path $reportDir ((Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString()+'.json')
    if ($Mode -eq 'Update') {
        $picker=New-Object Windows.Forms.OpenFileDialog
        $picker.Title='选择提供方新版 Hook DLL'
        $picker.Filter='Hook DLL (*.dll)|*.dll'
        if ($picker.ShowDialog() -ne 'OK') { exit 0 }
        $arguments=@('--hook-component-update',('"'+$picker.FileName+'"'))
    } else { $arguments=@('--hook-component-rollback') }
    $arguments += @('--confirm-auto-text-disabled','--write-report',('"'+$report+'"'))
    $process=Start-Process -FilePath $exe -WorkingDirectory $PSScriptRoot -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        $code='HOOK_COMPONENT_FAILED'
        if (Test-Path -LiteralPath $report) {
            $data=Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($data.error_code -match '^HOOK_[A-Z0-9_]+$') { $code=$data.error_code }
        }
        throw ('维护未完成，错误码：'+$code+'。请保留现场并导出故障证据；不要手动清理数据或凭证。')
    }
    $data=Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($data.status -notin @('installed_pending_validation','rolled_back')) { throw '组件结果未确认，请保留现场联系工程师。' }
    [void][Windows.Forms.MessageBox]::Show('组件和配置已同步，原数据及凭证保留。请从原“Work WeChat / 工作微信”快捷方式启动，使用原账号登录，再打开 Worker 完成环境和恢复检查。暂不开始接单，待语音和文字验收。',$caption,'OK','Information')
    $exitCode=0
} catch {
    [void][Windows.Forms.MessageBox]::Show([string]$_.Exception.Message,$caption,'OK','Error')
}
exit $exitCode
