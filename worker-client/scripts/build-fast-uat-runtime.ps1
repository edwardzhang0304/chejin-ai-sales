param(
  [string]$OutputDir = ".fast-uat-runtime",
  [string]$PythonVersion = "3.12.10"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$runtimeRoot = [System.IO.Path]::GetFullPath((Join-Path $Root $OutputDir))
$downloadDir = Join-Path $Root "dist\fast-uat-downloads"
$archivePath = Join-Path $downloadDir "python-embed-amd64.zip"
$pythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"

if (Test-Path $runtimeRoot) {
  Remove-Item -LiteralPath $runtimeRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $runtimeRoot, $downloadDir | Out-Null
Invoke-WebRequest -Uri $pythonUrl -OutFile $archivePath
Expand-Archive -LiteralPath $archivePath -DestinationPath $runtimeRoot -Force

python -m pip install --disable-pip-version-check --no-compile --target (Join-Path $runtimeRoot "Lib\site-packages") -r requirements.txt
if ($LASTEXITCODE -ne 0) {
  throw "Failed to install the portable Fast UAT runtime dependencies."
}

$pthPath = Get-ChildItem -Path $runtimeRoot -Filter "python*._pth" -File | Select-Object -First 1
if ($null -eq $pthPath) {
  throw "Embedded Python path configuration was not found."
}
@(
  "python312.zip",
  ".",
  "Lib\site-packages",
  "..\app",
  "import site"
) | Set-Content -LiteralPath $pthPath.FullName -Encoding ASCII

$requirementsHash = (Get-FileHash -Algorithm SHA256 (Join-Path $Root "requirements.txt")).Hash
$baseIdentity = [ordered]@{
  schema_version = 1
  runtime_kind = "chejin_worker_fast_uat_base"
  python_version = $PythonVersion
  platform = "windows-x64"
  requirements_sha256 = $requirementsHash
  reusable = $true
}
$baseIdentity | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runtimeRoot "fast-uat-runtime-base.json") -Encoding UTF8

& (Join-Path $runtimeRoot "python.exe") -c "import PySide6, onnxruntime, uiautomation; print('Fast UAT portable runtime probe passed')"
if ($LASTEXITCODE -ne 0) {
  throw "Fast UAT portable runtime probe failed."
}

Write-Host "Fast UAT portable runtime ready: $runtimeRoot"
