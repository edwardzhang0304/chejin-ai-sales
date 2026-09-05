param(
  [string]$PackageDir = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$UpdaterReadyTimeoutSeconds = 120
$BuildPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $BuildPython)) {
  throw "Updater process test requires the build virtual-environment Python"
}
if ($PackageDir -eq "") {
  $PackageDir = Join-Path $Root "dist\CheJinWorkerClient"
}
$UpdaterExe = Join-Path $PackageDir "CheJinUpdater.exe"
if (-not (Test-Path $UpdaterExe)) {
  throw "Updater process test requires the real packaged CheJinUpdater.exe"
}
if ([string]::IsNullOrWhiteSpace([string]$env:CHEJIN_RELEASE_SIGNING_PRIVATE_KEY_BASE64)) {
  throw "Updater process test requires the CI release signing private key"
}
if ([string]::IsNullOrWhiteSpace([string]$env:CHEJIN_RELEASE_SIGNING_KEY_ID)) {
  throw "Updater process test requires the release signing key id"
}

$TestRoot = Join-Path $env:RUNNER_TEMP "chejin-updater-real-process"
if (Test-Path $TestRoot) {
  Remove-Item -LiteralPath $TestRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $TestRoot | Out-Null

$OldWorkerSource = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

public static class OldWorkerProbe {
  public static int Main(string[] args) {
    string stopFile = "";
    string rollbackPlan = "";
    for (int i = 0; i < args.Length; i++) {
      if (args[i] == "--stop-file" && i + 1 < args.Length) stopFile = args[++i];
      else if (args[i] == "--post-rollback-plan" && i + 1 < args.Length) rollbackPlan = args[++i];
    }
    if (!String.IsNullOrEmpty(rollbackPlan)) {
      string control = Path.GetDirectoryName(Path.GetFullPath(rollbackPlan));
      File.WriteAllText(Path.Combine(control, "rollback-worker.pid"), Process.GetCurrentProcess().Id.ToString());
      Thread.Sleep(TimeSpan.FromMinutes(2));
      return 0;
    }
    if (String.IsNullOrEmpty(stopFile)) return 31;
    File.WriteAllText(stopFile + ".pid", Process.GetCurrentProcess().Id.ToString());
    DateTime deadline = DateTime.UtcNow.AddMinutes(2);
    while (DateTime.UtcNow < deadline && !File.Exists(stopFile)) Thread.Sleep(100);
    return File.Exists(stopFile) ? 0 : 32;
  }
}
'@

$HealthyWorkerSource = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

public static class HealthyWorkerProbe {
  private static string JsonString(string text, string key) {
    Match match = Regex.Match(text, "\\\"" + Regex.Escape(key) + "\\\"\\s*:\\s*\\\"((?:\\\\.|[^\\\"])*)\\\"");
    if (!match.Success) throw new InvalidDataException("missing " + key);
    return Regex.Unescape(match.Groups[1].Value);
  }
  private static string Sha256(string value) {
    using (SHA256 sha = SHA256.Create()) {
      byte[] hash = sha.ComputeHash(Encoding.UTF8.GetBytes(value));
      StringBuilder result = new StringBuilder();
      foreach (byte item in hash) result.Append(item.ToString("x2"));
      return result.ToString();
    }
  }
  public static int Main(string[] args) {
    string planPath = "";
    string token = "";
    for (int i = 0; i < args.Length; i++) {
      if (args[i] == "--post-update-plan" && i + 1 < args.Length) planPath = args[++i];
      else if (args[i] == "--post-update-token" && i + 1 < args.Length) token = args[++i];
    }
    if (String.IsNullOrEmpty(planPath) || String.IsNullOrEmpty(token)) return 41;
    string plan = File.ReadAllText(planPath, Encoding.UTF8);
    string marker = JsonString(plan, "healthy_marker_path");
    string requestId = JsonString(plan, "update_request_id");
    string targetVersion = JsonString(plan, "target_version");
    Directory.CreateDirectory(Path.GetDirectoryName(marker));
    string json = "{\"schema_version\":2,\"healthy\":true,\"version\":\"" + targetVersion + "\",\"update_request_id\":\"" + requestId + "\",\"one_time_token_sha256\":\"" + Sha256(token) + "\",\"runtime_health\":{\"ready\":true,\"binding_state\":\"bound\",\"ui_event_loop_alive\":true,\"required_threads\":[\"task_runner\",\"c2_listener\",\"thread_monitor\"],\"threads\":{\"task_runner\":{\"entered_loop\":true,\"alive\":true},\"c2_listener\":{\"entered_loop\":true,\"alive\":true},\"thread_monitor\":{\"entered_loop\":true,\"alive\":true}},\"startup_failures\":[],\"stable_sample_count\":3,\"stable_for_ms\":1250}}";
    File.WriteAllText(marker, json, new UTF8Encoding(false));
    File.WriteAllText(Path.Combine(Path.GetDirectoryName(planPath), "new-worker.pid"), Process.GetCurrentProcess().Id.ToString());
    Thread.Sleep(TimeSpan.FromMinutes(2));
    return 0;
  }
}
'@

$FailedWorkerSource = @'
public static class FailedWorkerProbe {
  public static int Main(string[] args) { return 47; }
}
'@

function New-ProbeExe([string]$Source, [string]$Destination) {
  $Parent = Split-Path -Parent $Destination
  New-Item -ItemType Directory -Force -Path $Parent | Out-Null
  Add-Type -TypeDefinition $Source -Language CSharp -OutputAssembly $Destination -OutputType ConsoleApplication
}

function Wait-File([string]$Path, [int]$Seconds = 30) {
  $Deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $Deadline) {
    if (Test-Path $Path) { return }
    Start-Sleep -Milliseconds 100
  }
  throw "Timed out waiting for $Path"
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
  [IO.File]::WriteAllText($Path, $Content, (New-Object Text.UTF8Encoding($false)))
}

function Read-UpdaterDiagnostic([string]$Path) {
  if (-not (Test-Path $Path)) { return "missing" }
  return ((Get-Content -Encoding UTF8 $Path | Select-Object -Last 20) -join " | ")
}

function Convert-SignedReleaseToClientIdentity([object]$ReleaseDescriptor) {
  # sign-client-release.py emits the immutable publication descriptor used by
  # the backend registration command.  The real latest-release API exposes
  # that version as latest_version, which is the shape persisted in an update
  # plan.  Exercise that production boundary instead of feeding the updater a
  # backend-only descriptor directly.
  $Identity = $ReleaseDescriptor | Select-Object *
  $Identity | Add-Member -NotePropertyName "latest_version" -NotePropertyValue ([string]$ReleaseDescriptor.version) -Force
  return $Identity
}

function Stop-ProbeFromPidFile([string]$PidFile) {
  if (-not (Test-Path $PidFile)) { return }
  $ProbePid = [int](Get-Content -Raw -Encoding UTF8 $PidFile)
  Stop-Process -Id $ProbePid -Force -ErrorAction SilentlyContinue
}

function New-ReleasePlan(
  [string]$CaseRoot,
  [string]$NewWorkerSource,
  [string]$RequestId
) {
  $Current = Join-Path $CaseRoot "install\CheJinWorkerClient"
  $Previous = Join-Path $CaseRoot "install\CheJinWorkerClient.previous"
  $Staged = Join-Path $CaseRoot "staging\CheJinWorkerClient"
  $Failed = Join-Path $CaseRoot "failed-program"
  $Data = Join-Path $CaseRoot "data"
  $Control = Join-Path $CaseRoot "control"
  foreach ($Directory in @($Current, $Staged, $Data, $Control)) {
    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
  }
  New-ProbeExe $OldWorkerSource (Join-Path $Current "CheJinWorkerClient.exe")
  Set-Content -LiteralPath (Join-Path $Current "version.txt") -Value "old" -Encoding ASCII
  New-ProbeExe $NewWorkerSource (Join-Path $Staged "CheJinWorkerClient.exe")
  Copy-Item -LiteralPath $UpdaterExe -Destination (Join-Path $Staged "CheJinUpdater.exe")
  Set-Content -LiteralPath (Join-Path $Staged "version.txt") -Value "new" -Encoding ASCII
  $PackageManifestPath = Join-Path $Staged "update-package-manifest.json"
  & $BuildPython (Join-Path $Root "scripts\generate-update-package-manifest.py") `
    --package-root $Staged `
    --version "0.9.61" `
    --git-commit ("b" * 40) `
    --output $PackageManifestPath | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Could not generate process-test package manifest" }
  $Archive = Join-Path $Control "client.zip"
  Compress-Archive -Path $Staged -DestinationPath $Archive -CompressionLevel Fastest -Force
  $PublishedAt = (Get-Date).ToUniversalTime().ToString("o")
  $ReleasePath = Join-Path $Control "release.json"
  & $BuildPython (Join-Path $Root "scripts\sign-client-release.py") `
    --archive $Archive `
    --package-manifest $PackageManifestPath `
    --version "0.9.61" `
    --git-commit ("b" * 40) `
    --artifact-storage-key "gray/windows-x64/process-test.zip" `
    --published-at $PublishedAt `
    --key-id $env:CHEJIN_RELEASE_SIGNING_KEY_ID `
    --output $ReleasePath | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Could not sign process-test release" }
  $ReleaseDescriptor = Get-Content -Raw -Encoding UTF8 $ReleasePath | ConvertFrom-Json
  $Release = Convert-SignedReleaseToClientIdentity $ReleaseDescriptor
  $RandomBytes = New-Object byte[] 32
  $Random = [Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $Random.GetBytes($RandomBytes)
  } finally {
    $Random.Dispose()
  }
  $Token = [Convert]::ToBase64String($RandomBytes)
  $TokenBytes = [Text.Encoding]::UTF8.GetBytes($Token)
  $TokenHash = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($TokenBytes)).Replace("-", "").ToLowerInvariant()
  $PlanPath = Join-Path $Control "update-plan.json"
  $Plan = [ordered]@{
    schema_version = 1
    update_request_id = $RequestId
    current_version = "0.9.59"
    target_version = "0.9.61"
    current_program_dir = $Current
    staged_program_dir = $Staged
    previous_program_dir = $Previous
    failed_program_dir = $Failed
    data_dir = $Data
    archive_path = $Archive
    healthy_marker_path = (Join-Path $Control "healthy.json")
    updater_ready_path = (Join-Path $Control "updater-ready.json")
    worker_executable_relative = "CheJinWorkerClient.exe"
    old_pid = 0
    old_exit_timeout_seconds = 20
    health_timeout_seconds = 20
    result_timeout_seconds = 60
    one_time_token_sha256 = $TokenHash
    release = $Release
    safe_boundary = [ordered]@{
      safe = $true
      new_work_blocked = $true
      backend_stopped_confirmed_or_unbound = $true
      confirmed_run_status = "paused"
      current_task = $null
      inflight_flow_id = $null
      task_lease_active = $false
      ui_lock_active = $false
      sidecar_active = $false
      waiting_ledger = 0
      pending_c2_outbox = 0
      pending_sqlite_action_journal = 0
      pending_file_action_journal = 0
      pending_sent_ack = 0
      action_journal_state_unavailable = 0
    }
  }
  Write-Utf8NoBom $PlanPath ($Plan | ConvertTo-Json -Depth 12)
  return [pscustomobject]@{
    Current = $Current
    Previous = $Previous
    Staged = $Staged
    Failed = $Failed
    Control = $Control
    PlanPath = $PlanPath
    Token = $Token
  }
}

function New-FormalClientReleasePlan(
  [string]$CaseRoot,
  [string]$RequestId
) {
  $Current = Join-Path $CaseRoot "install\CheJinWorkerClient"
  $Previous = Join-Path $CaseRoot "install\CheJinWorkerClient.previous"
  $Staged = Join-Path $CaseRoot "staging\CheJinWorkerClient"
  $Failed = Join-Path $CaseRoot "failed-program"
  $Data = Join-Path $CaseRoot "data"
  $Control = Join-Path $CaseRoot "control"
  foreach ($Directory in @($Current, $Staged, $Data, $Control)) {
    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
  }
  New-ProbeExe $OldWorkerSource (Join-Path $Current "CheJinWorkerClient.exe")
  Set-Content -LiteralPath (Join-Path $Current "version.txt") -Value "old" -Encoding ASCII
  Copy-Item -Path (Join-Path $PackageDir "*") -Destination $Staged -Recurse -Force
  $PackageManifestPath = Join-Path $Staged "update-package-manifest.json"
  if (-not (Test-Path $PackageManifestPath)) {
    throw "Formal package is missing update-package-manifest.json"
  }
  $PackageManifest = Get-Content -Raw -Encoding UTF8 $PackageManifestPath | ConvertFrom-Json
  $TargetVersion = [string]$PackageManifest.version
  $GitCommit = [string]$PackageManifest.git_commit
  if ($TargetVersion -ne "0.9.66") {
    throw "Formal process test expected package version 0.9.66, got $TargetVersion"
  }

  $OldWorkerHome = [Environment]::GetEnvironmentVariable("CHEJIN_WORKER_HOME", "Process")
  $OldPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
  try {
    $env:CHEJIN_WORKER_HOME = $Data
    $env:PYTHONPATH = $Root
    $SnapshotJson = & $BuildPython -c "import json; from chejin_worker_client.models import Binding; from chejin_worker_client.storage import save_binding; from chejin_worker_client.update_data_snapshot import protected_update_snapshot; save_binding(Binding('windows-process-worker','test-token','windows-process-instance',run_status='paused')); print(json.dumps(protected_update_snapshot(), ensure_ascii=False))"
    if ($LASTEXITCODE -ne 0) { throw "Could not create formal-client protected snapshot" }
    $ProtectedSnapshot = ($SnapshotJson -join "`n") | ConvertFrom-Json
  } finally {
    $env:CHEJIN_WORKER_HOME = $OldWorkerHome
    $env:PYTHONPATH = $OldPythonPath
  }

  $Archive = Join-Path $Control "client.zip"
  Compress-Archive -Path $Staged -DestinationPath $Archive -CompressionLevel Fastest -Force
  $PublishedAt = (Get-Date).ToUniversalTime().ToString("o")
  $ReleasePath = Join-Path $Control "release.json"
  & $BuildPython (Join-Path $Root "scripts\sign-client-release.py") `
    --archive $Archive `
    --package-manifest $PackageManifestPath `
    --version $TargetVersion `
    --git-commit $GitCommit `
    --artifact-storage-key "gray/windows-x64/formal-process-test.zip" `
    --published-at $PublishedAt `
    --key-id $env:CHEJIN_RELEASE_SIGNING_KEY_ID `
    --output $ReleasePath | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Could not sign formal process-test release" }
  $ReleaseDescriptor = Get-Content -Raw -Encoding UTF8 $ReleasePath | ConvertFrom-Json
  $Release = Convert-SignedReleaseToClientIdentity $ReleaseDescriptor

  $RandomBytes = New-Object byte[] 32
  $Random = [Security.Cryptography.RandomNumberGenerator]::Create()
  try { $Random.GetBytes($RandomBytes) } finally { $Random.Dispose() }
  $Token = [Convert]::ToBase64String($RandomBytes)
  $TokenBytes = [Text.Encoding]::UTF8.GetBytes($Token)
  $TokenHash = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($TokenBytes)).Replace("-", "").ToLowerInvariant()
  $PlanPath = Join-Path $Control "update-plan.json"
  $Plan = [ordered]@{
    schema_version = 1
    update_request_id = $RequestId
    current_version = "0.9.58"
    target_version = $TargetVersion
    current_program_dir = $Current
    staged_program_dir = $Staged
    previous_program_dir = $Previous
    failed_program_dir = $Failed
    data_dir = $Data
    archive_path = $Archive
    healthy_marker_path = (Join-Path $Control "healthy.json")
    updater_ready_path = (Join-Path $Control "updater-ready.json")
    worker_executable_relative = "CheJinWorkerClient.exe"
    old_pid = 0
    old_exit_timeout_seconds = 20
    health_timeout_seconds = 30
    result_timeout_seconds = 70
    one_time_token_sha256 = $TokenHash
    release = $Release
    protected_data_snapshot = $ProtectedSnapshot
    safe_boundary = [ordered]@{
      safe = $true
      new_work_blocked = $true
      backend_stopped_confirmed_or_unbound = $true
      confirmed_run_status = "paused"
      current_task = $null
      inflight_flow_id = $null
      task_lease_active = $false
      ui_lock_active = $false
      sidecar_active = $false
      waiting_ledger = 0
      pending_c2_outbox = 0
      pending_sqlite_action_journal = 0
      pending_file_action_journal = 0
      pending_sent_ack = 0
      action_journal_state_unavailable = 0
    }
    pre_update_run_status = "paused"
    operator_pause_after_request = $false
    fault_after_request = $false
  }
  Write-Utf8NoBom $PlanPath ($Plan | ConvertTo-Json -Depth 20)
  return [pscustomobject]@{
    Current = $Current
    Previous = $Previous
    Staged = $Staged
    Failed = $Failed
    Data = $Data
    Control = $Control
    PlanPath = $PlanPath
    Token = $Token
  }
}

function Invoke-UpdateCase([object]$Case, [string]$ExpectedState) {
  $StopFile = Join-Path $Case.Control "stop-old"
  $Old = Start-Process -FilePath (Join-Path $Case.Current "CheJinWorkerClient.exe") -ArgumentList @("--stop-file", $StopFile) -PassThru
  Wait-File ($StopFile + ".pid")
  $Plan = Get-Content -Raw -Encoding UTF8 $Case.PlanPath | ConvertFrom-Json
  $Plan.old_pid = $Old.Id
  Write-Utf8NoBom $Case.PlanPath ($Plan | ConvertTo-Json -Depth 12)
  $DiagnosticPath = Join-Path $Case.Control "updater-startup.jsonl"
  $PreviousDiagnosticPath = [Environment]::GetEnvironmentVariable("CHEJIN_UPDATER_DIAGNOSTIC_PATH", "Process")
  try {
    $env:CHEJIN_UPDATER_DIAGNOSTIC_PATH = $DiagnosticPath
    $Updater = Start-Process -FilePath $UpdaterExe -ArgumentList @("--plan", $Case.PlanPath, "--token", $Case.Token) -PassThru
  } finally {
    if ([string]::IsNullOrEmpty($PreviousDiagnosticPath)) {
      Remove-Item Env:CHEJIN_UPDATER_DIAGNOSTIC_PATH -ErrorAction SilentlyContinue
    } else {
      $env:CHEJIN_UPDATER_DIAGNOSTIC_PATH = $PreviousDiagnosticPath
    }
  }
  $ReadyPath = Join-Path $Case.Control "updater-ready.json"
  $ResultPath = Join-Path $Case.Control "update-result.json"
  $ReadyDeadline = (Get-Date).AddSeconds($UpdaterReadyTimeoutSeconds)
  while ((Get-Date) -lt $ReadyDeadline -and -not (Test-Path $ReadyPath)) {
    if (Test-Path $ResultPath) {
      $EarlyResult = Get-Content -Raw -Encoding UTF8 $ResultPath | ConvertFrom-Json
      throw "Updater rejected the plan before ready: state=$($EarlyResult.state), code=$($EarlyResult.result_code), message=$($EarlyResult.message), diagnostic=$(Read-UpdaterDiagnostic $DiagnosticPath)"
    }
    if ($Updater.HasExited) {
      throw "Updater exited before ready with code $($Updater.ExitCode) and no result; diagnostic=$(Read-UpdaterDiagnostic $DiagnosticPath)"
    }
    Start-Sleep -Milliseconds 100
  }
  if (-not (Test-Path $ReadyPath)) {
    throw "Timed out waiting for $ReadyPath; updater_pid=$($Updater.Id); diagnostic=$(Read-UpdaterDiagnostic $DiagnosticPath)"
  }
  Set-Content -LiteralPath $StopFile -Value "stop" -Encoding ASCII
  if (-not $Updater.WaitForExit(60000)) {
    Stop-Process -Id $Updater.Id -Force
    throw "Real updater process timed out"
  }
  Wait-File $ResultPath
  $Result = Get-Content -Raw -Encoding UTF8 $ResultPath | ConvertFrom-Json
  if ($Result.state -ne $ExpectedState) {
    throw "Expected updater state $ExpectedState, got $($Result.state): $($Result.message)"
  }
  return $Result
}

try {
  $Success = New-ReleasePlan (Join-Path $TestRoot "success") $HealthyWorkerSource "update-success"
  Invoke-UpdateCase $Success "succeeded" | Out-Null
  if ((Get-Content -Raw -Encoding ASCII (Join-Path $Success.Current "version.txt")).Trim() -ne "new") {
    throw "Success case did not switch to the new program directory"
  }
  if (-not (Test-Path $Success.Previous)) {
    throw "Success case did not retain exactly one previous program directory"
  }
  Stop-ProbeFromPidFile (Join-Path $Success.Control "new-worker.pid")

  $Rollback = New-ReleasePlan (Join-Path $TestRoot "rollback") $FailedWorkerSource "update-rollback"
  Invoke-UpdateCase $Rollback "rolled_back" | Out-Null
  if ((Get-Content -Raw -Encoding ASCII (Join-Path $Rollback.Current "version.txt")).Trim() -ne "old") {
    throw "Rollback case did not restore the old program directory"
  }
  if (-not (Test-Path (Join-Path $Rollback.Failed "CheJinWorkerClient.exe"))) {
    throw "Rollback case did not preserve the failed new program"
  }
  Stop-ProbeFromPidFile (Join-Path $Rollback.Control "rollback-worker.pid")

  $OldWorkerHome = [Environment]::GetEnvironmentVariable("CHEJIN_WORKER_HOME", "Process")
  $OldApiBaseUrl = [Environment]::GetEnvironmentVariable("CHEJIN_API_BASE_URL", "Process")
  $OldApiTimeout = [Environment]::GetEnvironmentVariable("CHEJIN_API_TIMEOUT", "Process")
  $OldRpaMode = [Environment]::GetEnvironmentVariable("CHEJIN_RPA_MODE", "Process")
  try {
    $Formal = New-FormalClientReleasePlan (Join-Path $TestRoot "formal-client") "update-formal-client"
    $env:CHEJIN_WORKER_HOME = $Formal.Data
    $env:CHEJIN_API_BASE_URL = "http://127.0.0.1:9/api"
    $env:CHEJIN_API_TIMEOUT = "0.2"
    $env:CHEJIN_RPA_MODE = "mock"
    Invoke-UpdateCase $Formal "succeeded" | Out-Null
    $Marker = Get-Content -Raw -Encoding UTF8 (Join-Path $Formal.Control "healthy.json") | ConvertFrom-Json
    if ($Marker.runtime_health.binding_state -ne "bound") {
      throw "Formal client health did not prove a bound runtime"
    }
    foreach ($ThreadName in @("task_runner", "c2_listener", "thread_monitor")) {
      $Thread = $Marker.runtime_health.threads.$ThreadName
      if (-not $Thread.entered_loop -or -not $Thread.alive) {
        throw "Formal client health did not prove live $ThreadName"
      }
    }
    Stop-Process -Id ([int]$Marker.pid) -Force -ErrorAction SilentlyContinue
  } finally {
    $env:CHEJIN_WORKER_HOME = $OldWorkerHome
    $env:CHEJIN_API_BASE_URL = $OldApiBaseUrl
    $env:CHEJIN_API_TIMEOUT = $OldApiTimeout
    $env:CHEJIN_RPA_MODE = $OldRpaMode
  }
  Write-Host "Real Windows updater process switch and rollback passed."
} finally {
  Get-Process -Name "CheJinWorkerClient" -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "$TestRoot*" } |
    Stop-Process -Force -ErrorAction SilentlyContinue
}
