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
$stdoutPath = Join-Path $probeRoot "operator_guard.stdout.log"
$stderrPath = Join-Path $probeRoot "operator_guard.stderr.log"
$tenantId = "packaged-operator-guard-probe"
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

New-Item -ItemType Directory -Force -Path $probeRoot | Out-Null
Write-ProbeJson -Path $controlPath -Payload @{
  version = 1
  tenant_id = $tenantId
  mode = "running"
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
    "--control-key", "f8",
    "--control-double-window-ms", "420",
    "--pause-poll-interval-ms", "120",
    "--allow-manual-input",
    "--floating-indicator",
    "--guard-state-path", $statePath
  )
  $guardProcess = Start-Process `
    -FilePath $ExePath `
    -ArgumentList $guardArguments `
    -WorkingDirectory $WorkingDirectory `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

  $deadline = [DateTime]::UtcNow.AddSeconds(30)
  while ([DateTime]::UtcNow -lt $deadline) {
    $guardProcess.Refresh()
    if ($guardProcess.HasExited) {
      Write-ProbeEvidence
      throw "packaged Operator Guard exited before becoming ready: exit_code=$($guardProcess.ExitCode)"
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
          $candidate.hooks_installed -eq $true -and
          $candidate.floating_indicator_requested -eq $true -and
          $candidate.floating_indicator_active -eq $true -and
          $candidate.floating_indicator_render_ok -eq $true
        ) {
          $readyState = $candidate
          break
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

  if ($null -eq $readyState) {
    Write-ProbeEvidence
    throw "packaged Operator Guard did not become ready within 30 seconds"
  }
  if ([int]$readyState.pid -ne [int]$guardProcess.Id) {
    throw "packaged Operator Guard state PID does not match the child process"
  }
  if ([int]$readyState.parent_pid -ne [int]$PID) {
    throw "packaged Operator Guard state parent PID does not match the probe process"
  }
  if ([string]::IsNullOrWhiteSpace([string]$readyState.floating_indicator_backend)) {
    throw "packaged Operator Guard did not report a floating indicator backend"
  }
  if ($readyState.block_manual_input -ne $false) {
    throw "packaged Operator Guard probe unexpectedly enabled manual-input blocking"
  }

  Write-Host "PACKAGED_OPERATOR_GUARD_READY $($readyState | ConvertTo-Json -Compress -Depth 8)"

  Write-ProbeJson -Path $controlPath -Payload @{
    version = 1
    tenant_id = $tenantId
    mode = "stopped"
    command = @{
      id = 1
      action = "stop"
      status = "pending"
      source = "packaged_probe"
      requested_at = [DateTime]::UtcNow.ToString("o")
      applied_at = ""
      message = "packaged_probe_complete"
    }
  }
  if (-not $guardProcess.WaitForExit(20000)) {
    throw "packaged Operator Guard did not stop after the control file requested shutdown"
  }
  if ($guardProcess.ExitCode -ne 0) {
    Write-ProbeEvidence
    throw "packaged Operator Guard exited with code $($guardProcess.ExitCode)"
  }

  $probePassed = $true
  Write-Host "Packaged Operator Guard probe passed."
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
