param(
  [Parameter(Mandatory = $true)]
  [string]$ApiBaseUrl
)

$ErrorActionPreference = "Stop"

$parsedUrl = $null
if (-not [Uri]::TryCreate($ApiBaseUrl, [UriKind]::Absolute, [ref]$parsedUrl) -or
    $parsedUrl.Scheme -notin @("http", "https")) {
  throw "API 地址无效，请填写完整的 http:// 或 https:// 地址。"
}

$normalizedApiBaseUrl = $ApiBaseUrl.TrimEnd("/")
$exePath = Join-Path $PSScriptRoot "车金Worker客户端.exe"
if (-not (Test-Path $exePath)) {
  throw "未找到车金Worker客户端.exe。请完整解压 ZIP，不要单独复制启动脚本或 EXE。"
}

$localAppData = [Environment]::GetFolderPath("LocalApplicationData")
if ([string]::IsNullOrWhiteSpace($localAppData)) {
  throw "无法读取 Windows LOCALAPPDATA，不能保存 UAT 预检报告。"
}
$diagnosticsDir = Join-Path $localAppData "CheJinWorker\diagnostics"
New-Item -ItemType Directory -Force -Path $diagnosticsDir | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportPath = Join-Path $diagnosticsDir "uat-preflight-$timestamp.json"

$env:CHEJIN_API_BASE_URL = $normalizedApiBaseUrl
$env:CHEJIN_RPA_MODE = "real"
$preflightArgs = @(
  "--preflight",
  "--preflight-format", "json",
  "--write-report", ('"' + $reportPath + '"')
)
$preflight = Start-Process -FilePath $exePath -ArgumentList $preflightArgs -Wait -PassThru
if ($preflight.ExitCode -ne 0) {
  Write-Host "UAT 预检未通过，客户端没有启动。" -ForegroundColor Red
  Write-Host "后端 API：$normalizedApiBaseUrl"
  Write-Host "预检报告：$reportPath"
  exit $preflight.ExitCode
}

Write-Host "UAT 预检通过。" -ForegroundColor Green
Write-Host "后端 API：$normalizedApiBaseUrl"
Write-Host "预检报告：$reportPath"
Start-Process -FilePath $exePath
