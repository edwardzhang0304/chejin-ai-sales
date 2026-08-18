param(
  [Parameter(Mandatory = $true)]
  [datetimeoffset]$From,
  [Parameter(Mandatory = $true)]
  [datetimeoffset]$To,
  [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

if ($To -le $From) {
  throw "-To must be later than -From."
}

$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Join-Path $packageRoot "collect_uat_evidence.py"
$python = Join-Path $packageRoot "runtime\python.exe"
$workerHome = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "CheJinWorker"

if (-not (Test-Path -LiteralPath $collector)) {
  throw "Evidence collector helper is missing: $collector"
}
if (-not (Test-Path -LiteralPath $python)) {
  throw "Bundled read-only evidence runtime is missing: $python"
}

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
  $desktop = [Environment]::GetFolderPath("Desktop")
  $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $OutputPath = Join-Path $desktop ("chejin-uat-evidence-" + $stamp + ".zip")
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)

& $python $collector `
  --app-dir $workerHome `
  --package-dir $packageRoot `
  --from-iso $From.ToUniversalTime().ToString("o") `
  --to-iso $To.ToUniversalTime().ToString("o") `
  --output $OutputPath

if ($LASTEXITCODE -ne 0) {
  throw "UAT evidence collection failed with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath $OutputPath)) {
  throw "UAT evidence archive was not created: $OutputPath"
}

Write-Host "UAT evidence archive created: $OutputPath" -ForegroundColor Green
