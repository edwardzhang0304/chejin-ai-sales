$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..\..")
Set-Location $ProjectRoot

$env:WECHAT_KNOWLEDGE_TENANT = "jiangsu_chejin_usedcar_customer_20260501"

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Config = "apps\wechat_ai_customer_service\configs\jiangsu_chejin_xucong_live.example.json"
$LogDir = Join-Path $ProjectRoot "runtime\apps\wechat_ai_customer_service\tenants\jiangsu_chejin_usedcar_customer_20260501\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LoopLog = Join-Path $LogDir "xucong_live_listener_loop.log"

while ($true) {
    $startedAt = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Add-Content -LiteralPath $LoopLog -Encoding UTF8 -Value "[$startedAt] listen_once_start"
    & $Python "apps\wechat_ai_customer_service\workflows\listen_and_reply.py" --config $Config --send --once *>> $LoopLog
    $exitCode = $LASTEXITCODE
    $finishedAt = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Add-Content -LiteralPath $LoopLog -Encoding UTF8 -Value "[$finishedAt] listen_once_exit=$exitCode"
    Start-Sleep -Seconds 3
}
