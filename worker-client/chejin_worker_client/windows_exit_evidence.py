"""Bounded read-only Windows event projection for the sole desktop export.

No event message/XML, dump, command line, account or full path is exported.
An event (or its absence) does not establish who terminated a process.
"""
import base64
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import time

from .performance_contract import POLICY

LOOKBACK_HOURS = POLICY.retention_seconds / 3600
EVENT_LIMIT = 200
MAX_OUTPUT = 512 * 1024
APPS = {'wechat.exe', 'chejinworkerclient.exe', 'chejindiagnostics.exe'}
FIELDS = {'AppName', 'AppVersion', 'ModuleName', 'ModuleVersion', 'ExceptionCode',
          'FaultingOffset', 'ProcessId', 'ProcessCreationTime', 'ReportId',
          'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7', 'P8',
          'SystemCommitLimit', 'SystemCommitCharge', 'PhysicalMemorySize'}
# Project named event fields, not localized free-form messages. The query is
# fixed and local, with count/time limits; it never changes event log settings.
SCRIPT = r'''
$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$since=(Get-Date).AddHours(-__LOOKBACK_HOURS__); $rows=@(); $queries=@()
foreach($spec in @(@{LogName='Application'; Id=@(1000,1001)}, @{LogName='System'; Id=2004; ProviderName='Microsoft-Windows-Resource-Exhaustion-Detector'})) {
 $filter=@{}; foreach($k in $spec.Keys){$filter[$k]=$spec[$k]}; $filter['StartTime']=$since
 try {
  $events=@(Get-WinEvent -FilterHashtable $filter -MaxEvents 200 -ErrorAction Stop)
  $queries+=@{log=$spec.LogName; status='collected'; inspected=$events.Count; limit_reached=($events.Count -ge 200)}
  foreach($e in $events) {
   [xml]$xml=$e.ToXml(); $fields=@{}
   foreach($d in @($xml.Event.EventData.Data)) {
    $name=[string]$d.Name
    if($name -in @('AppName','AppVersion','ModuleName','ModuleVersion','ExceptionCode','FaultingOffset','ProcessId','ProcessCreationTime','ReportId','P1','P2','P3','P4','P5','P6','P7','P8','SystemCommitLimit','SystemCommitCharge','PhysicalMemorySize')) {
     $v=[string]$d.'#text'; if($v.Length -gt 256){$v=$v.Substring(0,256)}; $fields[$name]=$v
    }
   }
   if($spec.LogName -eq 'System') {
    foreach($d in @($xml.SelectNodes('//*[local-name()="SystemInfo"]/*'))) {
     if($d.LocalName -in @('SystemCommitLimit','SystemCommitCharge','PhysicalMemorySize')) {
      $fields[$d.LocalName]=[string]$d.InnerText
     }
    }
   }
   if($spec.LogName -eq 'Application') {
    $app=$fields['AppName']; if(-not $app){$app=$fields['P1']}
    if([IO.Path]::GetFileName([string]$app).ToLowerInvariant() -notin @('wechat.exe','chejinworkerclient.exe','chejindiagnostics.exe')) {continue}
   }
   $rows+=@{log=$spec.LogName; provider=[string]$e.ProviderName; id=[int]$e.Id; record_id=$e.RecordId; time_utc=$e.TimeCreated.ToUniversalTime().ToString('o'); fields=$fields}
  }
 } catch {
  $status='read_failed'; if($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*'){$status='absent'}
  $queries+=@{log=$spec.LogName; status=$status; error_type=$_.Exception.GetType().Name}
 }
}
@{events=@($rows); queries=@($queries)} | ConvertTo-Json -Depth 6 -Compress
'''


def project(raw):
    """Second explicit allowlist before the common ZIP sanitizer."""
    result = {'lookback_hours': LOOKBACK_HOURS, 'max_events_per_log': EVENT_LIMIT,
              'scope': 'Application 1000/1001 for Worker or WeChat; System resource exhaustion 2004',
              'interpretation': 'No event is not proof of no crash; resource events do not prove causation.',
              'queries': [], 'events': []}
    for q in raw['queries'][:2]:
        result['queries'].append({k: q[k] for k in ('log','status','inspected','limit_reached','error_type') if k in q})
    for event in raw['events'][:2 * EVENT_LIMIT]:
        fields = event.get('fields') or {}
        if event.get('log') == 'Application':
            if event.get('id') not in (1000, 1001):
                continue
            app = str(fields.get('AppName') or fields.get('P1') or '')
            if PureWindowsPath(app).name.lower() not in APPS:
                continue
        elif not (event.get('log') == 'System' and event.get('id') == 2004
                  and event.get('provider') == 'Microsoft-Windows-Resource-Exhaustion-Detector'):
            continue
        safe = {}
        for key in FIELDS & fields.keys():
            value = str(fields[key])[:256]
            # Fault module/version/exception fields are sufficient; strip paths
            # wherever a provider unexpectedly includes one in an allowed field.
            if '\\' in value or '/' in value:
                value = PureWindowsPath(value).name
            safe[key] = value
        result['events'].append({**{k:event.get(k) for k in ('log','provider','id','record_id','time_utc')}, 'fields':safe})
    return result


def collect(collector, archive):
    source = 'windows/events'
    if sys.platform != 'win32':
        collector.record(source, 'not_applicable', reason='windows_only')
        return
    remaining = collector.deadline - time.monotonic()
    if collector.limited() or remaining <= 0.25:
        collector.record(source, 'omitted', reason='export_time_or_file_budget')
        return
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = SCRIPT.replace('__LOOKBACK_HOURS__', str(LOOKBACK_HOURS))
    command = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    try:
        completed = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
                                    '-EncodedCommand', command], capture_output=True,
                                   timeout=min(8, remaining), creationflags=0x08000000)
        if completed.returncode or len(completed.stdout) > MAX_OUTPUT:
            raise ValueError('event_query_failed_or_oversize')
        raw = json.loads(completed.stdout.decode('utf-8-sig'))
        if not isinstance(raw.get('queries'), list) or not isinstance(raw.get('events'), list):
            raise ValueError('event_query_schema')
        result = project(raw)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, subprocess.TimeoutExpired) as exc:
        collector.record(source, 'read_failed', reason='windows_event_query_unavailable', error_type=type(exc).__name__)
        return
    collector.visited += 1
    collector.add(archive, 'windows/events.json', json.dumps(collector.clean(result), ensure_ascii=False).encode('utf-8'), source)
    for query in result['queries']:
        if query.get('status') == 'read_failed':
            collector.record(source, 'read_failed', reason='event_log_unavailable', details=query)
        elif query.get('limit_reached'):
            collector.record(source, 'omitted', reason='event_scan_limit', details=query)
        elif query.get('status') == 'absent':
            collector.record(source, 'absent', details=query)
