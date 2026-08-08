param(
  [Parameter(Mandatory = $true)]
  [string]$ExePath,
  [string]$WorkingDirectory = ""
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
  throw "packaged Operator Guard probe executable not found: $ExePath"
}
if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
  $WorkingDirectory = Split-Path -Parent $ExePath
}

$probeRoot = Join-Path $env:TEMP ("chejin-operator-guard-probe-" + [guid]::NewGuid().ToString("N"))
$controlPath = Join-Path $probeRoot "operator_guard.control.json"
$statusPath = Join-Path $probeRoot "runtime.status.json"
$statePath = Join-Path $probeRoot "operator_guard.state.json"
$heartbeatAPath = Join-Path $probeRoot "operator_guard.heartbeat.0.json"
$heartbeatBPath = Join-Path $probeRoot "operator_guard.heartbeat.1.json"
$stdoutPath = Join-Path $probeRoot "operator_guard.stdout.log"
$stderrPath = Join-Path $probeRoot "operator_guard.stderr.log"
$tenantId = "packaged-operator-guard-probe"
$guardInstanceId = [guid]::NewGuid().ToString()
$clientInstanceId = "packaged-probe-" + [guid]::NewGuid().ToString("N")
$ownerStartUtc = (Get-Process -Id $PID).StartTime.ToUniversalTime()
$unixEpochUtc = [DateTime]::SpecifyKind([DateTime]"1970-01-01", [DateTimeKind]::Utc)
$ownerProcessCreateTime = ($ownerStartUtc - $unixEpochUtc).TotalSeconds
$ownerProcessCreateTimeText = $ownerProcessCreateTime.ToString("R", [Globalization.CultureInfo]::InvariantCulture)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$guardProcess = $null
$readyState = $null
$probePassed = $false

function Write-ProbeJson {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [object]$Payload
  )

  $json = $Payload | ConvertTo-Json -Depth 8
  [System.IO.File]::WriteAllText($Path, $json, $utf8NoBom)
}

function Write-ProbeEvidence {
  if (Test-Path -LiteralPath $statePath) {
    Write-Host "OPERATOR_GUARD_STATE_BEGIN"
    Get-Content -Raw -Encoding UTF8 $statePath | Write-Host
    Write-Host "OPERATOR_GUARD_STATE_END"
  }
  if (Test-Path -LiteralPath $stdoutPath) {
    Write-Host "OPERATOR_GUARD_STDOUT_BEGIN"
    Get-Content -Raw -Encoding UTF8 $stdoutPath | Write-Host
    Write-Host "OPERATOR_GUARD_STDOUT_END"
  }
  if (Test-Path -LiteralPath $stderrPath) {
    Write-Host "OPERATOR_GUARD_STDERR_BEGIN"
    Get-Content -Raw -Encoding UTF8 $stderrPath | Write-Host
    Write-Host "OPERATOR_GUARD_STDERR_END"
  }
}

function Get-LatestGuardHeartbeat {
  $candidates = @()
  foreach ($path in @($heartbeatAPath, $heartbeatBPath)) {
    if (-not (Test-Path -LiteralPath $path)) {
      continue
    }
    try {
      $payload = Get-Content -Raw -Encoding UTF8 $path | ConvertFrom-Json
      $heartbeatAt = [DateTimeOffset]::Parse([string]$payload.heartbeat_at)
      $candidates += [PSCustomObject]@{
        Payload = $payload
        HeartbeatAt = $heartbeatAt
      }
    }
    catch {
      # Alternating files mean one slot may be replaced while it is read.
    }
  }
  return $candidates | Sort-Object HeartbeatAt -Descending | Select-Object -First 1
}

function Wait-GuardState {
  param(
    [Parameter(Mandatory = $true)]
    [string]$ExpectedMode,
    [Parameter(Mandatory = $true)]
    [bool]$ExpectedLocked,
    [Parameter(Mandatory = $true)]
    [int]$ExpectedEpoch,
    [string]$ExpectedLockId = "",
    [int]$ExpectedFencingToken = 0,
    [int]$TimeoutSeconds = 30
  )

  $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
  while ([DateTime]::UtcNow -lt $deadline) {
    $guardProcess.Refresh()
    if ($guardProcess.HasExited) {
      Write-ProbeEvidence
      throw "packaged Operator Guard exited while waiting for mode $ExpectedMode"
    }
    if (Test-Path -LiteralPath $statePath) {
      try {
        $candidate = Get-Content -Raw -Encoding UTF8 $statePath | ConvertFrom-Json
        if ([string]$candidate.phase -eq "failed") {
          Write-ProbeEvidence
          throw "packaged Operator Guard reported failure: $([string]$candidate.reason)"
        }
        if (
          [string]$candidate.phase -eq "running" -and
          [string]$candidate.mode -eq $ExpectedMode -and
          [bool]$candidate.lock_enabled -eq $ExpectedLocked -and
          [int]$candidate.control_epoch -eq $ExpectedEpoch -and
          [string]$candidate.active_ui_lock_id -eq $ExpectedLockId -and
          [int]$candidate.active_fencing_token -eq $ExpectedFencingToken -and
          [string]$candidate.guard_instance_id -eq $guardInstanceId -and
          $candidate.hooks_installed -eq $true -and
          $candidate.floating_indicator_active -eq $true -and
          $candidate.floating_indicator_render_ok -eq $true
        ) {
          return $candidate
        }
      }
      catch {
        if ($_.Exception.Message -like "packaged Operator Guard reported failure:*") {
          throw
        }
      }
    }
    Start-Sleep -Milliseconds 100
  }
  Write-ProbeEvidence
  throw "packaged Operator Guard did not reach mode $ExpectedMode within $TimeoutSeconds seconds"
}

New-Item -ItemType Directory -Force -Path $probeRoot | Out-Null
Write-ProbeJson -Path $controlPath -Payload @{
  version = 2
  tenant_id = $tenantId
  guard_instance_id = $guardInstanceId
  client_instance_id = $clientInstanceId
  owner_worker_pid = $PID
  owner_process_create_time = $ownerProcessCreateTime
  mode = "idle"
  active_ui_lock_id = ""
  active_fencing_token = 0
  operation_type = ""
  current_step = ""
  control_epoch = 0
  shutdown_requested = $false
  command = @{
    id = 0
    action = "none"
    status = "idle"
    source = "packaged_probe"
    requested_at = ""
    applied_at = ""
    message = ""
  }
}
Write-ProbeJson -Path $statusPath -Payload @{
  ok = $true
  state = "thinking"
  message = "packaged_operator_guard_probe"
  tenant_id = $tenantId
}

try {
  $guardArguments = @(
    "--rpa-operator-guard",
    "--tenant-id", $tenantId,
    "--control-path", $controlPath,
    "--status-path", $statusPath,
    "--parent-pid", [string]$PID,
    "--guard-instance-id", $guardInstanceId,
    "--client-instance-id", $clientInstanceId,
    "--owner-process-create-time", $ownerProcessCreateTimeText,
    "--control-key", "f8",
    "--control-double-window-ms", "420",
    "--pause-poll-interval-ms", "120",
    "--block-manual-input",
    "--floating-indicator",
    "--guard-state-path", $statePath,
    "--heartbeat-path-a", $heartbeatAPath,
    "--heartbeat-path-b", $heartbeatBPath
  )
  $guardProcess = Start-Process `
    -FilePath $ExePath `
    -ArgumentList $guardArguments `
    -WorkingDirectory $WorkingDirectory `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

  $readyState = Wait-GuardState -ExpectedMode "idle" -ExpectedLocked $false -ExpectedEpoch 0
  if ([int]$readyState.pid -ne [int]$guardProcess.Id) {
    throw "packaged Operator Guard state PID does not match the child process"
  }
  if ([int]$readyState.parent_pid -ne [int]$PID) {
    throw "packaged Operator Guard state parent PID does not match the probe process"
  }
  if ([string]::IsNullOrWhiteSpace([string]$readyState.floating_indicator_backend)) {
    throw "packaged Operator Guard did not report a floating indicator backend"
  }
  if ($readyState.block_manual_input -ne $true) {
    throw "packaged Operator Guard probe did not enable active-mode input protection"
  }

  $guardPid = [int]$guardProcess.Id
  Write-Host "PACKAGED_OPERATOR_GUARD_IDLE $($readyState | ConvertTo-Json -Compress -Depth 8)"

  Write-ProbeJson -Path $controlPath -Payload @{
    version = 2
    tenant_id = $tenantId
    guard_instance_id = $guardInstanceId
    client_instance_id = $clientInstanceId
    owner_worker_pid = $PID
    owner_process_create_time = $ownerProcessCreateTime
    mode = "active"
    active_ui_lock_id = "packaged-probe-lock"
    active_fencing_token = 1
    operation_type = "packaged_probe"
    current_step = "active_probe"
    control_epoch = 1
    shutdown_requested = $false
    command = @{ id = 1; action = "activate"; status = "pending"; source = "packaged_probe"; requested_at = [DateTime]::UtcNow.ToString("o"); applied_at = ""; message = "verify_active_lock" }
  }
  $activeState = Wait-GuardState -ExpectedMode "active" -ExpectedLocked $true -ExpectedEpoch 1 -ExpectedLockId "packaged-probe-lock" -ExpectedFencingToken 1
  if ([int]$activeState.pid -ne $guardPid) {
    throw "packaged Operator Guard changed process during activation"
  }
  Write-Host "PACKAGED_OPERATOR_GUARD_ACTIVE $($activeState | ConvertTo-Json -Compress -Depth 8)"

  Write-ProbeJson -Path $controlPath -Payload @{
    version = 2
    tenant_id = $tenantId
    guard_instance_id = $guardInstanceId
    client_instance_id = $clientInstanceId
    owner_worker_pid = $PID
    owner_process_create_time = $ownerProcessCreateTime
    mode = "ready"
    active_ui_lock_id = ""
    active_fencing_token = 0
    operation_type = ""
    current_step = ""
    control_epoch = 2
    shutdown_requested = $false
    command = @{ id = 2; action = "deactivate"; status = "pending"; source = "packaged_probe"; requested_at = [DateTime]::UtcNow.ToString("o"); applied_at = ""; message = "verify_unlock" }
  }
  $unlockedState = Wait-GuardState -ExpectedMode "ready" -ExpectedLocked $false -ExpectedEpoch 2
  if ([int]$unlockedState.pid -ne $guardPid) {
    throw "packaged Operator Guard changed process during deactivation"
  }
  Write-Host "PACKAGED_OPERATOR_GUARD_READY $($unlockedState | ConvertTo-Json -Compress -Depth 8)"

  # Reproduce the Windows UAT failure: evidence collection/antivirus may hold
  # the state file open without FILE_SHARE_DELETE. The guard must keep running
  # and publish a fresh heartbeat immediately after the reader releases it.
  $heartbeatBeforeContention = [DateTimeOffset]::Parse([string]$unlockedState.heartbeat_at)
  $stateReader = [System.IO.File]::Open(
    $statePath,
    [System.IO.FileMode]::Open,
    [System.IO.FileAccess]::Read,
    [System.IO.FileShare]::Read
  )
  try {
    Start-Sleep -Milliseconds 2300
    $guardProcess.Refresh()
    if ($guardProcess.HasExited) {
      throw "packaged Operator Guard exited while its state file was read-locked"
    }
    $liveHeartbeat = Get-LatestGuardHeartbeat
    if ($null -eq $liveHeartbeat) {
      throw "packaged Operator Guard did not publish an independent heartbeat"
    }
    $heartbeatAgeMs = ([DateTimeOffset]::UtcNow - $liveHeartbeat.HeartbeatAt).TotalMilliseconds
    if (
      $heartbeatAgeMs -ge 1000 -or
      [int]$liveHeartbeat.Payload.pid -ne $guardPid -or
      [string]$liveHeartbeat.Payload.guard_instance_id -ne $guardInstanceId
    ) {
      throw "packaged Operator Guard independent heartbeat became stale during state-file contention: $heartbeatAgeMs ms"
    }
    Write-Host "PACKAGED_OPERATOR_GUARD_INDEPENDENT_HEARTBEAT_FRESH $heartbeatAgeMs"
  }
  finally {
    $stateReader.Dispose()
  }
  $contentionDeadline = [DateTime]::UtcNow.AddSeconds(5)
  $contentionState = $null
  while ([DateTime]::UtcNow -lt $contentionDeadline) {
    try {
      $candidate = Get-Content -Raw -Encoding UTF8 $statePath | ConvertFrom-Json
      $candidateHeartbeat = [DateTimeOffset]::Parse([string]$candidate.heartbeat_at)
      if (
        [int]$candidate.pid -eq $guardPid -and
        [string]$candidate.mode -eq "ready" -and
        $candidateHeartbeat -gt $heartbeatBeforeContention
      ) {
        $contentionState = $candidate
        break
      }
    }
    catch {
      # The writer may be in the middle of the first post-contention replace.
    }
    Start-Sleep -Milliseconds 50
  }
  if ($null -eq $contentionState) {
    Write-ProbeEvidence
    throw "packaged Operator Guard did not recover its heartbeat after state-file contention"
  }
  Write-Host "PACKAGED_OPERATOR_GUARD_STATE_CONTENTION_RECOVERED $($contentionState | ConvertTo-Json -Compress -Depth 8)"

  Write-ProbeJson -Path $controlPath -Payload @{
    version = 2
    tenant_id = $tenantId
    guard_instance_id = $guardInstanceId
    client_instance_id = $clientInstanceId
    owner_worker_pid = $PID
    owner_process_create_time = $ownerProcessCreateTime
    mode = "stopped"
    active_ui_lock_id = ""
    active_fencing_token = 0
    operation_type = ""
    current_step = ""
    control_epoch = 3
    shutdown_requested = $false
    command = @{
      id = 3
      action = "stop"
      status = "pending"
      source = "packaged_probe"
      requested_at = [DateTime]::UtcNow.ToString("o")
      applied_at = ""
      message = "packaged_probe_complete"
    }
  }
  $stoppedState = Wait-GuardState -ExpectedMode "stopped" -ExpectedLocked $false -ExpectedEpoch 3
  $guardProcess.Refresh()
  if ($guardProcess.HasExited -or [int]$stoppedState.pid -ne $guardPid) {
    throw "packaged Operator Guard did not remain resident in stopped mode"
  }
  Write-Host "PACKAGED_OPERATOR_GUARD_STOPPED_RESIDENT $($stoppedState | ConvertTo-Json -Compress -Depth 8)"

  $shutdownControl = Get-Content -Raw -Encoding UTF8 $controlPath | ConvertFrom-Json
  $shutdownControl.shutdown_requested = $true
  $shutdownControl.control_epoch = 4
  $shutdownControl.command = @{ id = 4; action = "shutdown"; status = "pending"; source = "packaged_probe"; requested_at = [DateTime]::UtcNow.ToString("o"); applied_at = ""; message = "worker_exit_probe" }
  Write-ProbeJson -Path $controlPath -Payload $shutdownControl
  if (-not $guardProcess.WaitForExit(20000)) {
    throw "packaged Operator Guard did not exit after explicit Worker shutdown"
  }
  $guardProcess.WaitForExit()
  $guardProcess.Refresh()
  $finalState = Get-Content -Raw -Encoding UTF8 $statePath | ConvertFrom-Json
  if (
    [string]$finalState.phase -ne "stopped" -or
    [string]$finalState.reason -ne "guard_exit" -or
    [int]$finalState.pid -ne [int]$guardProcess.Id
  ) {
    Write-ProbeEvidence
    throw "packaged Operator Guard did not leave a clean final state"
  }
  $exitCode = $guardProcess.ExitCode
  if ($null -ne $exitCode -and [int]$exitCode -ne 0) {
    Write-ProbeEvidence
    throw "packaged Operator Guard exited with code $exitCode"
  }
  if ($null -eq $exitCode) {
    Write-Host "PACKAGED_OPERATOR_GUARD_EXIT_CODE unavailable_on_powershell_5_1; clean guard_exit state verified"
  }
  else {
    Write-Host "PACKAGED_OPERATOR_GUARD_EXIT_CODE $exitCode"
  }

  $probePassed = $true
  Write-Host "Packaged Operator Guard resident lifecycle probe passed."
}
finally {
  if ($null -ne $guardProcess) {
    $guardProcess.Refresh()
    if (-not $guardProcess.HasExited) {
      Stop-Process -Id $guardProcess.Id -Force -ErrorAction SilentlyContinue
    }
  }
  if (-not $probePassed) {
    Write-ProbeEvidence
    Write-Host "Packaged Operator Guard probe evidence directory: $probeRoot"
  }
  else {
    Remove-Item -LiteralPath $probeRoot -Recurse -Force -ErrorAction SilentlyContinue
  }
}
