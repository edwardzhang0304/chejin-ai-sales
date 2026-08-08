param()

$ErrorActionPreference = "Stop"

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$desktop = [Environment]::GetFolderPath("Desktop")
$staging = Join-Path $env:TEMP ("chejin-uat-evidence-" + $timestamp)
$output = Join-Path $desktop ("chejin-uat-evidence-" + $timestamp + ".zip")
$guardRoot = Join-Path $PSScriptRoot "_internal\omniauto-rpa\runtime\apps\wechat_ai_customer_service\tenants\default\rpa_operator_guard"
$incidentRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "CheJinWorker\incidents"
$diagnosticsRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "CheJinWorker\diagnostics"

New-Item -ItemType Directory -Force -Path $staging | Out-Null

if (Test-Path -LiteralPath $guardRoot) {
  $guardDestination = Join-Path $staging "rpa_operator_guard"
  & robocopy.exe $guardRoot $guardDestination /E /R:3 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -ge 8) {
    throw "Failed to collect Operator Guard evidence."
  }
}

if (Test-Path -LiteralPath $incidentRoot) {
  $incidentDestination = Join-Path $staging "incidents"
  New-Item -ItemType Directory -Force -Path $incidentDestination | Out-Null
  $incidents = @(
    Get-ChildItem -LiteralPath $incidentRoot -File -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -like "INC-*" } |
      Sort-Object LastWriteTime -Descending |
      Select-Object -First 8
  )
  foreach ($incident in $incidents) {
    Copy-Item -LiteralPath $incident.FullName -Destination $incidentDestination -Force
  }
}

if (Test-Path -LiteralPath $diagnosticsRoot) {
  $diagnosticsDestination = Join-Path $staging "diagnostics"
  & robocopy.exe $diagnosticsRoot $diagnosticsDestination /E /R:3 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -ge 8) {
    throw "Failed to collect startup diagnostics."
  }
}

$evidenceFiles = @(Get-ChildItem -LiteralPath $staging -File -Recurse -ErrorAction SilentlyContinue)
if ($evidenceFiles.Count -eq 0) {
  throw "No UAT evidence files were found. Keep the Worker open after the failure and run this script again."
}

Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $output -Force
if (-not (Test-Path -LiteralPath $output)) {
  throw "UAT evidence archive was not created."
}

Write-Host "UAT evidence archive created: $output" -ForegroundColor Green
explorer.exe /select,$output
