param(
  [switch]$SkipTests,
  [switch]$SkipPreflight,
  [switch]$DevelopmentBuild,
  [string]$ApiBaseUrl = ""
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$ReportsDir = Join-Path $Root "dist\reports"
$PackageDir = Join-Path $Root "dist\车金Worker客户端"
$ExePath = Join-Path $PackageDir "车金Worker客户端.exe"
$ManifestPath = Join-Path $ReportsDir "车金Worker客户端.manifest.json"
$PreflightReportPath = Join-Path $ReportsDir "preflight-build-report.json"
$PackagingDiagnosticPath = Join-Path $ReportsDir "packaging-runtime-diagnostics.jsonl"
$UatLauncherSourcePath = Join-Path $Root "packaging\start-uat.ps1"
$UatLauncherPath = Join-Path $PackageDir "start-uat.ps1"
$UatLauncherValidatorPath = Join-Path $Root "scripts\validate-uat-launcher.ps1"
$OmniAutoSourcePath = Join-Path $Root "omniauto-rpa"
$OmniAutoProvenancePath = Join-Path $OmniAutoSourcePath ".chejin-source.json"
$OmniAutoSidecarPath = Join-Path $OmniAutoSourcePath "apps\wechat_ai_customer_service\adapters\wechat_win32_ocr_sidecar.py"
$GeneratedObservationSchemaPath = Join-Path $OmniAutoSourcePath "apps\wechat_ai_customer_service\adapters\chejin_c2_observation_schema.generated.json"
$TestsStatus = "not_run"
$PreflightStatus = "not_run"
$BuildSourceArgs = @("scripts\build_source.py")
if ($DevelopmentBuild) {
  $BuildSourceArgs += "--development-build"
}
$BuildSourceJson = & python @BuildSourceArgs
if ($LASTEXITCODE -ne 0) {
  throw "正式打包失败：缺少有效 Git 来源或合同文件。$BuildSourceJson"
}
$BuildSource = $BuildSourceJson | ConvertFrom-Json
if ($BuildSource.ok -ne $true) {
  throw "正式打包失败：源码身份检查未通过。"
}
$GitCommit = [string]$BuildSource.git_commit
$GitBranch = [string]$BuildSource.git_branch
$GitDirty = [bool]$BuildSource.git_dirty
$SourceContractPath = [string]$BuildSource.contract_path

if (-not $DevelopmentBuild -and $GitDirty) {
  throw "正式打包失败：Git 工作区存在未提交修改。调试包请显式使用 -DevelopmentBuild。"
}
if (-not $DevelopmentBuild -and $SkipTests) {
  throw "正式打包失败：不允许跳过测试。"
}
if (-not $DevelopmentBuild -and $SkipPreflight) {
  throw "正式打包失败：不允许跳过 Preflight。"
}
if (-not (Test-Path $OmniAutoProvenancePath)) {
  throw "打包失败：缺少 OmniAuto 来源说明 $OmniAutoProvenancePath"
}
$OmniAutoProvenance = Get-Content -Raw -Encoding UTF8 $OmniAutoProvenancePath | ConvertFrom-Json
$OmniAutoProvenanceSchema = [int]$OmniAutoProvenance.schema_version
$OmniAutoUpstreamBaseCommit = [string]$OmniAutoProvenance.upstream_base_commit
$OmniAutoChejinIntegrationCommit = $GitCommit.Trim()
$OmniAutoSelectiveIntegrations = @($OmniAutoProvenance.selective_integrations)
$OmniAutoHistoricalIntegrations = @($OmniAutoProvenance.historical_integrations)
$OmniAutoChejinOverlays = @($OmniAutoProvenance.chejin_overlays)
if ($OmniAutoUpstreamBaseCommit -notmatch '^[0-9a-fA-F]{40}$') {
  throw "打包失败：OmniAuto upstream base commit 不合法"
}
if ($OmniAutoChejinIntegrationCommit -notmatch '^[0-9a-fA-F]{40}$') {
  throw "打包失败：OmniAuto chejin integration commit 不合法"
}
foreach ($Integration in $OmniAutoSelectiveIntegrations) {
  if ([string]$Integration.source_commit -notmatch '^[0-9a-fA-F]{40}$') {
    throw "打包失败：OmniAuto selective source commit 不合法"
  }
  if (@($Integration.scope).Count -lt 1) {
    throw "打包失败：OmniAuto selective integration scope 不能为空"
  }
}
if ($OmniAutoProvenanceSchema -ge 3) {
  if ($OmniAutoChejinOverlays.Count -lt 1) {
    throw "打包失败：OmniAuto chejin overlays 不能为空"
  }
  if ($OmniAutoHistoricalIntegrations.Count -lt 1) {
    throw "打包失败：OmniAuto historical integrations 不能为空"
  }
  foreach ($Integration in $OmniAutoHistoricalIntegrations) {
    if ([string]$Integration.upstream_base_commit -notmatch '^[0-9a-fA-F]{40}$' -or
        [string]$Integration.source_commit -notmatch '^[0-9a-fA-F]{40}$' -or
        [string]$Integration.chejin_integration_commit -notmatch '^[0-9a-fA-F]{40}$') {
      throw "打包失败：OmniAuto historical integration commit 不合法"
    }
    if (@($Integration.scope).Count -lt 1) {
      throw "打包失败：OmniAuto historical integration scope 不能为空"
    }
  }
} elseif ($OmniAutoSelectiveIntegrations.Count -lt 1) {
  throw "打包失败：OmniAuto selective integrations 不能为空"
}

if (-not (Test-Path ".venv")) {
  python -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：pip 升级失败"
}
.\.venv\Scripts\pip.exe install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：依赖安装失败"
}
.\.venv\Scripts\python.exe -c "import uiautomation; print('uiautomation import passed')"
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：uiautomation 导入失败"
}
$RapidOcrSourceProbe = & .\.venv\Scripts\python.exe -c "import json; import onnxruntime; from PIL import Image; from rapidocr_onnxruntime import RapidOCR; assert onnxruntime.__version__ == '1.20.1', onnxruntime.__version__; image = Image.new('RGB', (96, 48), 'white'); items, _ = RapidOCR()(image); image.close(); print(json.dumps({'ok': True, 'onnxruntime_version': onnxruntime.__version__, 'ocr_item_count': len(items or [])}))"
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：源码环境无法初始化固定版本的图片复核 OCR。$RapidOcrSourceProbe"
}

if (-not $SkipTests) {
  .\.venv\Scripts\python.exe run_checks.py
  if ($LASTEXITCODE -ne 0) {
    throw "打包失败：Worker 完整测试未通过"
  }
  $TestsStatus = "passed"
}

New-Item -ItemType Directory -Force -Path $ReportsDir | Out-Null

if (-not $SkipPreflight) {
  $env:CHEJIN_RPA_MODE = "mock"
  $env:CHEJIN_WORKER_HOME = Join-Path $Root ".tmp-build-preflight"
  if ($ApiBaseUrl -ne "") {
    $env:CHEJIN_API_BASE_URL = $ApiBaseUrl
  }
  .\.venv\Scripts\python.exe -m chejin_worker_client.main --preflight --skip-backend --skip-wechat --preflight-format json --write-report $PreflightReportPath
  if ($LASTEXITCODE -ne 0) {
    throw "打包失败：Preflight 未通过"
  }
  $PreflightStatus = "passed"
}

if (-not (Test-Path $OmniAutoSidecarPath)) {
  throw "打包失败：当前分支未找到 OmniAuto sidecar $OmniAutoSidecarPath"
}
if (-not (Test-Path $GeneratedObservationSchemaPath)) {
  throw "打包失败：缺少生成的 C2 observation schema"
}
.\.venv\Scripts\python.exe scripts\generate-c2-observation-schema.py --check
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：C2 observation schema 校验失败"
}
$env:CHEJIN_OMNIAUTO_RPA_SOURCE = $OmniAutoSourcePath
$OmniAutoSourceSidecarHash = Get-FileHash -Algorithm SHA256 $OmniAutoSidecarPath
$GeneratedObservationSchemaHash = Get-FileHash -Algorithm SHA256 $GeneratedObservationSchemaPath
$OmniAutoSourceTreeJson = & .\.venv\Scripts\python.exe scripts\verify-omniauto-tree.py --source $OmniAutoSourcePath
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：OmniAuto 源码树校验失败"
}
$OmniAutoSourceTree = $OmniAutoSourceTreeJson | ConvertFrom-Json

$Version = .\.venv\Scripts\python.exe -c "from chejin_worker_client import __version__; print(__version__)"
$RuntimeBuildIdentityPath = Join-Path $ReportsDir "runtime-build-identity.json"
@{
  version = $Version.Trim()
  git_commit = $GitCommit.Trim()
  git_branch = $GitBranch.Trim()
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $RuntimeBuildIdentityPath
$env:CHEJIN_BUILD_IDENTITY_PATH = $RuntimeBuildIdentityPath

.\.venv\Scripts\pyinstaller.exe --clean --noconfirm packaging\chejin-worker-client.spec
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：PyInstaller 构建失败"
}

if (-not (Test-Path $ExePath)) {
  throw "打包失败：未找到 $ExePath"
}
if (-not (Test-Path $UatLauncherSourcePath)) {
  throw "打包失败：未找到 UAT 启动脚本 $UatLauncherSourcePath"
}
if (-not (Test-Path $UatLauncherValidatorPath)) {
  throw "打包失败：未找到 UAT 启动脚本校验器 $UatLauncherValidatorPath"
}
Copy-Item -LiteralPath $UatLauncherSourcePath -Destination $UatLauncherPath -Force
& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $UatLauncherValidatorPath -ScriptPath $UatLauncherPath
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：UAT 启动脚本未通过 Windows PowerShell 5.1 BOM/语法门禁"
}
$env:CHEJIN_PACKAGING_DIAGNOSTIC_PATH = $PackagingDiagnosticPath
if (Test-Path $PackagingDiagnosticPath) {
  Remove-Item -Force $PackagingDiagnosticPath
}
$BundledSidecarProbe = Start-Process -FilePath $ExePath -ArgumentList @("--omniauto-sidecar", "--help") -Wait -PassThru
if ($BundledSidecarProbe.ExitCode -ne 0) {
  if (Test-Path $PackagingDiagnosticPath) {
    Get-Content -Raw -Encoding UTF8 $PackagingDiagnosticPath | Write-Host
  }
  throw "打包失败：最终 exe 无法启动内置 OmniAuto sidecar"
}
$BundledVisionOcrProbe = Start-Process -FilePath $ExePath -ArgumentList @("--omniauto-ocr-probe") -Wait -PassThru
if ($BundledVisionOcrProbe.ExitCode -ne 0) {
  if (Test-Path $PackagingDiagnosticPath) {
    Get-Content -Raw -Encoding UTF8 $PackagingDiagnosticPath | Write-Host
  }
  throw "打包失败：最终 exe 无法启动图片复核 OCR 独立进程"
}
$PackagedPythonArchiveLines = & .\.venv\Scripts\pyi-archive_viewer.exe -l -r $ExePath
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：无法读取最终 exe 的 Python 归档"
}
$PackagedPythonArchive = $PackagedPythonArchiveLines -join "`n"
$RequiredArchiveModules = @(
  "uiautomation",
  "PIL.ImageEnhance",
  "PIL.ImageGrab",
  "rapidocr_onnxruntime",
  "pyperclip",
  "pywinauto"
)
foreach ($RequiredArchiveModule in $RequiredArchiveModules) {
  if ($PackagedPythonArchive -notmatch [regex]::Escape($RequiredArchiveModule)) {
    throw "打包失败：最终 exe 未包含运行依赖 $RequiredArchiveModule"
  }
}

$SidecarCandidates = @(
  (Join-Path $PackageDir "_internal\omniauto-rpa\apps\wechat_ai_customer_service\adapters\wechat_win32_ocr_sidecar.py"),
  (Join-Path $PackageDir "omniauto-rpa\apps\wechat_ai_customer_service\adapters\wechat_win32_ocr_sidecar.py")
)
$GeneratedSchemaCandidates = @(
  (Join-Path $PackageDir "_internal\omniauto-rpa\apps\wechat_ai_customer_service\adapters\chejin_c2_observation_schema.generated.json"),
  (Join-Path $PackageDir "omniauto-rpa\apps\wechat_ai_customer_service\adapters\chejin_c2_observation_schema.generated.json")
)
$SidecarPath = $SidecarCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $SidecarPath) {
  throw "打包失败：dist 产物中未找到 OmniAuto wechat_win32_ocr_sidecar.py"
}
$PackagedSidecarHash = Get-FileHash -Algorithm SHA256 $SidecarPath
if ($PackagedSidecarHash.Hash -ne $OmniAutoSourceSidecarHash.Hash) {
  throw "打包失败：dist 中 OmniAuto sidecar 与当前分支源码不一致"
}
$PackagedGeneratedSchemaPath = $GeneratedSchemaCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $PackagedGeneratedSchemaPath) {
  throw "打包失败：dist 产物中未找到生成的 C2 observation schema"
}
$PackagedGeneratedSchemaHash = Get-FileHash -Algorithm SHA256 $PackagedGeneratedSchemaPath
if ($PackagedGeneratedSchemaHash.Hash -ne $GeneratedObservationSchemaHash.Hash) {
  throw "打包失败：dist 中生成的 C2 observation schema 与当前合同不一致"
}
$PackagedOmniAutoCandidates = @(
  (Join-Path $PackageDir "_internal\omniauto-rpa"),
  (Join-Path $PackageDir "omniauto-rpa")
)
$PackagedOmniAutoPath = $PackagedOmniAutoCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $PackagedOmniAutoPath) {
  throw "打包失败：dist 产物中未找到完整 OmniAuto 目录"
}
$ClientBoundaryJson = & .\.venv\Scripts\python.exe scripts\client_delivery_policy.py --omniauto-root $OmniAutoSourcePath --scan-root $PackagedOmniAutoPath
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：客户端产物包含服务器私有文件。$ClientBoundaryJson"
}
$ClientBoundary = $ClientBoundaryJson | ConvertFrom-Json
if ($ClientBoundary.ok -ne $true) {
  throw "打包失败：客户端交付边界检查未通过。"
}
$OmniAutoTreeVerificationJson = & .\.venv\Scripts\python.exe scripts\verify-omniauto-tree.py --source $OmniAutoSourcePath --packaged $PackagedOmniAutoPath
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：最终产物 OmniAuto 树校验失败"
}
$OmniAutoTreeVerification = $OmniAutoTreeVerificationJson | ConvertFrom-Json

$Hash = Get-FileHash -Algorithm SHA256 $ExePath
$Files = Get-ChildItem $PackageDir -Recurse -File
$TotalBytes = ($Files | Measure-Object -Property Length -Sum).Sum
$ContractRevision = .\.venv\Scripts\python.exe -c "from chejin_worker_client.c2_contract import contract_revision; print(contract_revision())"
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：无法读取 C2 合同版本"
}
$ContractCanonicalSha256 = .\.venv\Scripts\python.exe -c "from chejin_worker_client.c2_contract import contract_sha256; print(contract_sha256())"
if ($LASTEXITCODE -ne 0) {
  throw "打包失败：无法读取 C2 合同哈希"
}
$PackagedContractCandidates = @(
  (Join-Path $PackageDir "_internal\contracts\c2_contract_v3.json"),
  (Join-Path $PackageDir "contracts\c2_contract_v3.json")
)
$PackagedContractPath = $PackagedContractCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not (Test-Path $SourceContractPath)) {
  throw "打包失败：源码合同文件不存在 $SourceContractPath"
}
if (-not $PackagedContractPath) {
  throw "打包失败：dist 产物中未找到 C2 合同文件"
}
$SourceContractHash = Get-FileHash -Algorithm SHA256 $SourceContractPath
$PackagedContractHash = Get-FileHash -Algorithm SHA256 $PackagedContractPath
if ($SourceContractHash.Hash -ne $PackagedContractHash.Hash) {
  throw "打包失败：源码合同与安装包合同 SHA256 不一致"
}

$Manifest = [ordered]@{
  app_name = "车金Worker客户端"
  version = $Version.Trim()
  built_at = (Get-Date).ToUniversalTime().ToString("o")
  package_dir = $PackageDir
  exe_path = $ExePath
  exe_sha256 = $Hash.Hash
  git_commit = $GitCommit.Trim()
  git_branch = $GitBranch.Trim()
  git_dirty = $GitDirty
  build_kind = if ($DevelopmentBuild) { "development" } else { "official" }
  formal_release = -not $DevelopmentBuild
  tests_status = $TestsStatus
  preflight_status = $PreflightStatus
  c2_contract_revision = $ContractRevision.Trim()
  c2_contract_sha256 = $PackagedContractHash.Hash
  c2_contract_path = $PackagedContractPath
  source_c2_contract_sha256 = $SourceContractHash.Hash
  canonical_c2_contract_sha256 = $ContractCanonicalSha256.Trim()
  c2_contract_file_check = "passed"
  generated_observation_schema_sha256 = $GeneratedObservationSchemaHash.Hash
  packaged_generated_observation_schema_sha256 = $PackagedGeneratedSchemaHash.Hash
  omniauto_upstream_base_commit = $OmniAutoUpstreamBaseCommit
  omniauto_selective_integrations = $OmniAutoSelectiveIntegrations
  omniauto_historical_integrations = $OmniAutoHistoricalIntegrations
  omniauto_chejin_overlays = $OmniAutoChejinOverlays
  omniauto_chejin_integration_commit = $OmniAutoChejinIntegrationCommit
  omniauto_source_path = $OmniAutoSourcePath
  omniauto_source_tree_sha256 = $OmniAutoTreeVerification.source.tree_sha256
  omniauto_source_file_count = $OmniAutoTreeVerification.source.file_count
  packaged_omniauto_tree_sha256 = $OmniAutoTreeVerification.packaged.tree_sha256
  packaged_omniauto_file_count = $OmniAutoTreeVerification.packaged.file_count
  omniauto_tree_check = "passed"
  client_delivery_boundary_check = "passed"
  omniauto_source_sidecar_sha256 = $OmniAutoSourceSidecarHash.Hash
  sidecar_path = $SidecarPath
  packaged_sidecar_sha256 = $PackagedSidecarHash.Hash
  file_count = $Files.Count
  package_bytes = $TotalBytes
  preflight_report = if (Test-Path $PreflightReportPath) { $PreflightReportPath } else { $null }
}

$Manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $ManifestPath

Write-Host "Built: $ExePath"
Write-Host "SHA256: $($Hash.Hash)"
Write-Host "Manifest: $ManifestPath"
