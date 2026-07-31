param(
  [switch]$SkipTests,
  [switch]$SkipPreflight,
  [switch]$DevelopmentBuild,
  [string]$ApiBaseUrl = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$ReportsDir = Join-Path $Root "dist\reports"
$PackageDir = Join-Path $Root "dist\车金Worker客户端"
$ExePath = Join-Path $PackageDir "车金Worker客户端.exe"
$ManifestPath = Join-Path $ReportsDir "车金Worker客户端.manifest.json"
$PreflightReportPath = Join-Path $ReportsDir "preflight-build-report.json"
$OmniAutoSourcePath = Join-Path $Root "omniauto-rpa"
$OmniAutoProvenancePath = Join-Path $OmniAutoSourcePath ".chejin-source.json"
$OmniAutoSidecarPath = Join-Path $OmniAutoSourcePath "apps\wechat_ai_customer_service\adapters\wechat_win32_ocr_sidecar.py"
$GeneratedObservationSchemaPath = Join-Path $OmniAutoSourcePath "apps\wechat_ai_customer_service\adapters\chejin_c2_observation_schema.generated.json"
$TestsStatus = "not_run"
$PreflightStatus = "not_run"
$GitCommit = (git rev-parse HEAD 2>$null)
$GitBranch = (git branch --show-current 2>$null)
$GitDirty = [bool](git status --porcelain 2>$null)

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
$OmniAutoUpstreamBaseCommit = [string]$OmniAutoProvenance.upstream_base_commit
$OmniAutoChejinIntegrationCommit = [string]$OmniAutoProvenance.chejin_integration_commit
$OmniAutoSelectiveIntegrations = @($OmniAutoProvenance.selective_integrations)
if ($OmniAutoUpstreamBaseCommit -notmatch '^[0-9a-fA-F]{40}$') {
  throw "打包失败：OmniAuto upstream base commit 不合法"
}
if ($OmniAutoChejinIntegrationCommit -notmatch '^[0-9a-fA-F]{40}$') {
  throw "打包失败：OmniAuto chejin integration commit 不合法"
}
if ($OmniAutoSelectiveIntegrations.Count -lt 1) {
  throw "打包失败：OmniAuto selective integrations 不能为空"
}
foreach ($Integration in $OmniAutoSelectiveIntegrations) {
  if ([string]$Integration.source_commit -notmatch '^[0-9a-fA-F]{40}$') {
    throw "打包失败：OmniAuto selective source commit 不合法"
  }
  if (@($Integration.scope).Count -lt 1) {
    throw "打包失败：OmniAuto selective integration scope 不能为空"
  }
}

if (-not (Test-Path ".venv")) {
  python -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\python.exe -c "import uiautomation; print('uiautomation import passed')"

if (-not $SkipTests) {
  .\.venv\Scripts\python.exe run_checks.py
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
  $PreflightStatus = "passed"
}

if (-not (Test-Path $OmniAutoSidecarPath)) {
  throw "打包失败：当前分支未找到 OmniAuto sidecar $OmniAutoSidecarPath"
}
if (-not (Test-Path $GeneratedObservationSchemaPath)) {
  throw "打包失败：缺少生成的 C2 observation schema"
}
.\.venv\Scripts\python.exe scripts\generate-c2-observation-schema.py --check
$env:CHEJIN_OMNIAUTO_RPA_SOURCE = $OmniAutoSourcePath
$OmniAutoSourceSidecarHash = Get-FileHash -Algorithm SHA256 $OmniAutoSidecarPath
$GeneratedObservationSchemaHash = Get-FileHash -Algorithm SHA256 $GeneratedObservationSchemaPath
$OmniAutoSourceTree = (
  .\.venv\Scripts\python.exe scripts\verify-omniauto-tree.py --source $OmniAutoSourcePath
) | ConvertFrom-Json

.\.venv\Scripts\pyinstaller.exe --clean --noconfirm packaging\chejin-worker-client.spec

if (-not (Test-Path $ExePath)) {
  throw "打包失败：未找到 $ExePath"
}
$BundledSidecarProbe = Start-Process -FilePath $ExePath -ArgumentList @("--omniauto-sidecar", "--help") -Wait -PassThru
if ($BundledSidecarProbe.ExitCode -ne 0) {
  throw "打包失败：最终 exe 无法启动内置 OmniAuto sidecar"
}
$PackagedPythonArchive = (
  .\.venv\Scripts\pyi-archive_viewer.exe -l -r $ExePath
) -join "`n"
if ($PackagedPythonArchive -notmatch 'uiautomation') {
  throw "打包失败：最终 exe 未包含 Windows UIA 诊断所需的 uiautomation"
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
$OmniAutoTreeVerification = (
  .\.venv\Scripts\python.exe scripts\verify-omniauto-tree.py --source $OmniAutoSourcePath --packaged $PackagedOmniAutoPath
) | ConvertFrom-Json

$Hash = Get-FileHash -Algorithm SHA256 $ExePath
$Files = Get-ChildItem $PackageDir -Recurse -File
$TotalBytes = ($Files | Measure-Object -Property Length -Sum).Sum
$Version = .\.venv\Scripts\python.exe -c "from chejin_worker_client import __version__; print(__version__)"
$ContractRevision = .\.venv\Scripts\python.exe -c "from chejin_worker_client.c2_contract import contract_revision; print(contract_revision())"
$ContractCanonicalSha256 = .\.venv\Scripts\python.exe -c "from chejin_worker_client.c2_contract import contract_sha256; print(contract_sha256())"
$SourceContractPath = Join-Path (Split-Path -Parent $Root) "contracts\c2_contract_v3.json"
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
  omniauto_chejin_integration_commit = $OmniAutoChejinIntegrationCommit
  omniauto_source_path = $OmniAutoSourcePath
  omniauto_source_tree_sha256 = $OmniAutoTreeVerification.source.tree_sha256
  omniauto_source_file_count = $OmniAutoTreeVerification.source.file_count
  packaged_omniauto_tree_sha256 = $OmniAutoTreeVerification.packaged.tree_sha256
  packaged_omniauto_file_count = $OmniAutoTreeVerification.packaged.file_count
  omniauto_tree_check = "passed"
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
