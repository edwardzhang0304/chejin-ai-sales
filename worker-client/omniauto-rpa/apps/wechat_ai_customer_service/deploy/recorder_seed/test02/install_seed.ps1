param(
  [string]$WorkspaceRoot = "D:\AI\omniauto",
  [bool]$ApplyGlobalRecorderModules = $true,
  [string]$SeedZipName = ""
)

$ErrorActionPreference = "Stop"

$seedRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$zipPath = ""

if (![string]::IsNullOrWhiteSpace($SeedZipName)) {
  $candidate = Join-Path $seedRoot $SeedZipName
  if (!(Test-Path $candidate)) {
    throw "Specified seed package not found: $candidate"
  }
  $zipPath = $candidate
}
else {
  $latest = Get-ChildItem -LiteralPath $seedRoot -File -Filter "test02_recorder_only_*.zip" `
    | Sort-Object LastWriteTime -Descending `
    | Select-Object -First 1
  if ($null -eq $latest) {
    throw "No seed package matched pattern test02_recorder_only_*.zip under: $seedRoot"
  }
  $zipPath = $latest.FullName
}

$tempRoot = Join-Path $env:TEMP ("test02_recorder_seed_" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

try {
  Expand-Archive -LiteralPath $zipPath -DestinationPath $tempRoot -Force
  $importScript = Join-Path $tempRoot "import_test02_recorder_payload.ps1"
  if (!(Test-Path $importScript)) {
    throw "Import script not found in package: $importScript"
  }

  $importScriptContent = Get-Content -LiteralPath $importScript -Raw -Encoding utf8
  if ($importScriptContent -match "ApplyGlobalRecorderModules") {
    & $importScript -WorkspaceRoot $WorkspaceRoot -ApplyGlobalRecorderModules:$ApplyGlobalRecorderModules
  }
  else {
    & $importScript -WorkspaceRoot $WorkspaceRoot
  }
}
finally {
  if (Test-Path $tempRoot) {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
  }
}
