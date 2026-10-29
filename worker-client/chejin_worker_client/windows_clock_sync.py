"""Explicit Windows repair; fixed elevated code, revocable per-operation ticket."""
import base64
import ctypes
from pathlib import Path
import subprocess
import sys

from .clock_sync_ticket import valid_operation

# No executable or NTP host is supplied by the UI/backend. The helper only
# reads its authorization ticket; it never executes user-writable script files.
HELPER = r'''
$ErrorActionPreference = 'Stop'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
$ticket = $null
$locked = $false
try {
  $ticket = [IO.File]::Open(TICKET_PATH, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
  $ticket.Lock(0, 1)
  $locked = $true
  $bytes = New-Object byte[] 128
  $count = $ticket.Read($bytes, 0, $bytes.Length)
  if ([Text.Encoding]::ASCII.GetString($bytes, 0, $count) -cne OPERATION_ID) { exit 27 }
  if ((Get-CimInstance Win32_ComputerSystem).PartOfDomain -or
      (Test-Path 'HKLM:\SOFTWARE\Policies\Microsoft\W32Time')) { exit 20 }
  $service = Get-Service W32Time
  if ($service.StartType -eq 'Disabled') { exit 21 }
  Set-TimeZone -Id 'China Standard Time'
  Start-Service W32Time
  $exe = Join-Path ([Environment]::GetFolderPath('System')) 'w32tm.exe'
  & $exe /config /manualpeerlist:ntp.aliyun.com,0x8 /syncfromflags:manual /update | Out-Null
  if ($LASTEXITCODE -ne 0) { exit 22 }
  & $exe /resync /rediscover /nowait | Out-Null
  if ($LASTEXITCODE -ne 0) { exit 23 }
  exit 0
} catch { exit 24 }
finally {
  if ($locked) { $ticket.Unlock(0, 1) }
  if ($null -ne $ticket) { $ticket.Dispose() }
}
'''


def encoded(script):
    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')


def powershell_path():
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer)):
        raise OSError('SYSTEM_DIRECTORY_UNAVAILABLE')
    return Path(buffer.value) / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'


def helper_script(operation, ticket_path):
    if not valid_operation(operation):
        raise ValueError('CLOCK_OPERATION_INVALID')
    # Single-quoted PowerShell literals: paths containing quotes/$() cannot
    # become executable expressions, even in a nonstandard deployment folder.
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    return HELPER.replace('TICKET_PATH', quote(ticket_path)).replace('OPERATION_ID', quote(operation))


def synchronize_windows(operation, ticket_path):
    if sys.platform != 'win32':
        return {'result': 'unsupported', 'helper_exit_confirmed': True}
    script = r'''
$ErrorActionPreference = 'Stop'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
try {
  $exe = Join-Path $PSHOME 'powershell.exe'
  $p = Start-Process -FilePath $exe -Verb RunAs -Wait -PassThru -ArgumentList '-NoProfile -NonInteractive -EncodedCommand ENCODED'
  exit $p.ExitCode
} catch {
  if ($_.Exception.NativeErrorCode -eq 1223 -or $_.Exception.InnerException.NativeErrorCode -eq 1223) { exit 25 }
  exit 26
}
'''.replace('ENCODED', encoded(helper_script(operation, ticket_path)))
    # No timeout can abandon a still-running elevated helper. UI remains on its
    # own thread. Ticket revocation additionally handles a killed parent/UAC.
    result = subprocess.run([str(powershell_path()), '-NoProfile', '-NonInteractive',
                             '-EncodedCommand', encoded(script)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=0x08000000, check=False)
    return {'result': {0: 'requested', 20: 'managed', 21: 'service_disabled',
            22: 'configure_failed', 23: 'resync_failed', 24: 'windows_failed',
            25: 'cancelled', 26: 'elevation_failed', 27: 'revoked'}.get(result.returncode, 'windows_failed'),
            'exit_code': result.returncode,
            'helper_exit_confirmed': result.returncode in {0, 20, 21, 22, 23, 24, 27},
            'partial_change_possible': result.returncode not in {20, 21, 25, 27}}
