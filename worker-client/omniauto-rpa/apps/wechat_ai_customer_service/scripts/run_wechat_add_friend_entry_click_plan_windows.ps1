param(
    [string]$Python = "",
    [string]$ArtifactDir = "",
    [string]$Phone = "",
    [string]$Wechat = "",
    [string]$VerifyMessage = "",
    [string]$RemarkName = "",
    [string]$RemarkCode = "",
    [switch]$AllowRenderRecovery,
    [switch]$NormalizeWindow
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..\..")
Set-Location $ProjectRoot

if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Error "Python executable not found: $Python. Create .venv first, or pass -Python C:\path\to\python.exe"
    exit 2
}

if ([string]::IsNullOrWhiteSpace($Phone) -and [string]::IsNullOrWhiteSpace($Wechat)) {
    Write-Error "Phone or Wechat is required. Example: .\apps\wechat_ai_customer_service\scripts\run_wechat_add_friend_entry_click_plan_windows.ps1 -Phone '17368746889' -VerifyMessage '我是车金二手车张伟' -RemarkName 'CJ-张伟-CJ8K2P-6889' -RemarkCode 'CJ8K2P'"
    exit 2
}

if ([string]::IsNullOrWhiteSpace($VerifyMessage)) {
    Write-Error "VerifyMessage is required for add-friend-entry-click-plan-windows."
    exit 2
}

if ([string]::IsNullOrWhiteSpace($RemarkName)) {
    Write-Error "RemarkName is required for add-friend-entry-click-plan-windows."
    exit 2
}

if ([string]::IsNullOrWhiteSpace($RemarkCode)) {
    Write-Error "RemarkCode is required for add-friend-entry-click-plan-windows."
    exit 2
}

if (-not $RemarkName.Contains($RemarkCode)) {
    Write-Error "RemarkName must include RemarkCode for add-friend-entry-click-plan-windows."
    exit 2
}

if ([string]::IsNullOrWhiteSpace($ArtifactDir)) {
    $Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $ArtifactDir = Join-Path $ProjectRoot "runtime\add_friend_entry_click_plan_windows\$Timestamp"
}
New-Item -ItemType Directory -Force -Path $ArtifactDir | Out-Null

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:WECHAT_WIN32_OCR_PASSIVE_PROBE = "0"
$env:WECHAT_WIN32_OCR_AGGRESSIVE_FOCUS = "1"
$env:WECHAT_WIN32_OCR_ATTACH_THREAD_INPUT = "1"
$env:WECHAT_WIN32_OCR_ACTIVATE_DEBOUNCE_SECONDS = "0"
$env:WECHAT_WIN32_OCR_WINDOW_NORMALIZE = $(if ($NormalizeWindow) { "1" } else { "0" })
$env:WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO = $(if ($AllowRenderRecovery) { "1" } else { "0" })

$Sidecar = "apps\wechat_ai_customer_service\adapters\wechat_win32_ocr_sidecar.py"
$StdoutPath = Join-Path $ArtifactDir "add_friend_entry_click_plan_stdout.json"
$StderrPath = Join-Path $ArtifactDir "add_friend_entry_click_plan_stderr.log"

$Args = @(
    $Sidecar,
    "add-friend-entry-click-plan-windows",
    "--artifact-dir",
    $ArtifactDir
)
if (-not [string]::IsNullOrWhiteSpace($Phone)) {
    $Args += @("--phone", $Phone)
}
if (-not [string]::IsNullOrWhiteSpace($Wechat)) {
    $Args += @("--wechat", $Wechat)
}
$Args += @("--verify-message", $VerifyMessage)
$Args += @("--remark-name", $RemarkName)
$Args += @("--remark-code", $RemarkCode)

Write-Host "ProjectRoot: $ProjectRoot"
Write-Host "ArtifactDir: $ArtifactDir"
Write-Host "Running Worker-facing Windows add_friend entry click route."

$NativeErrorActionPreference = "Continue"
$PreviousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Python @Args 1> $StdoutPath 2> $StderrPath
$ExitCode = $LASTEXITCODE
$ErrorActionPreference = $PreviousErrorActionPreference

Write-Host "ExitCode: $ExitCode"
Write-Host "Stdout: $StdoutPath"
Write-Host "Stderr: $StderrPath"
Write-Host "PlanJson: $(Join-Path $ArtifactDir 'add_friend_entry_click_plan.json')"
Write-Host "ReviewHtml: $(Join-Path $ArtifactDir 'add_friend_entry_click_review.html')"
Write-Host "ReviewJson: $(Join-Path $ArtifactDir 'add_friend_entry_click_review.json')"
$ReviewHtml = Join-Path $ArtifactDir "add_friend_entry_click_review.html"
Write-Host ("OpenReviewCommand: Start-Process -FilePath ""{0}""" -f $ReviewHtml)

$LatestDir = Join-Path $ProjectRoot "runtime\add_friend_entry_click_plan_windows\latest"
if (Test-Path -LiteralPath $LatestDir) {
    Remove-Item -LiteralPath $LatestDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $LatestDir | Out-Null
Copy-Item -Path (Join-Path $ArtifactDir "*") -Destination $LatestDir -Recurse -Force
$LatestReviewHtml = Join-Path $LatestDir "add_friend_entry_click_review.html"
Write-Host "LatestReviewHtml: $LatestReviewHtml"
Write-Host ("OpenLatestReviewCommand: Start-Process -FilePath ""{0}""" -f $LatestReviewHtml)

if (Test-Path -LiteralPath $StdoutPath) {
    Get-Content -LiteralPath $StdoutPath -Raw -Encoding UTF8
}

exit $ExitCode
