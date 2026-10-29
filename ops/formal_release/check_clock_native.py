"""Disposable GitHub Windows acceptance; no sales machine or real WeChat."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'worker-client'))
from chejin_worker_client.clock_sync_ticket import ClockSyncTicket
from chejin_worker_client.windows_clock_sync import powershell_path, encoded, helper_script, synchronize_windows
PRODUCT = '79fcf01691ff23fbabbf2f3d91d38d5a9f7af220'

class NativeFailure(RuntimeError):
    def __init__(self, code, returncode=None):
        self.code, self.returncode = code, returncode
        super().__init__(code)

def failure(exc, stage):
    value = {'stage': stage, 'type': type(exc).__name__}
    if isinstance(exc, NativeFailure):
        value.update(code=exc.code, returncode=exc.returncode)
    elif isinstance(exc, subprocess.TimeoutExpired):
        value.update(code='NATIVE_TIMEOUT', timeout_seconds=exc.timeout)
    elif isinstance(exc, OSError):
        value.update(code='OS_OPERATION_FAILED', errno=exc.errno, winerror=getattr(exc, 'winerror', None))
    else:
        value['code'] = 'CHECK_FAILED'
    return value  # Deliberately excludes raw commands, native stderr and paths.

class Evidence:
    def __init__(self, root):
        self.path = root / 'result.json'
        self.data = {'status': 'running', 'stage': 'identity', 'product_commit': PRODUCT,
                     'tool_commit': os.environ.get('GITHUB_SHA'), 'run_id': os.environ.get('GITHUB_RUN_ID'),
                     'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'), 'checks': {},
                     'primary_error': None, 'cleanup_errors': [], 'report_errors': [],
                     'clock_sync': None, 'real_wechat': False,
                     'real_uac_prompt': 'not certified on CI administrator desktop',
                     'disposable_vm_changes': 'NTP peer/service settings discarded with hosted VM; never run on retained computers.'}
        self.save()

    def save(self):
        try:
            tmp = self.path.with_suffix('.tmp')
            with tmp.open('w', encoding='utf8') as stream:
                json.dump(self.data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            self.data['status'] = 'failed'
            self.data['report_errors'].append(failure(exc, 'write_checkpoint'))
            print('CHECKPOINT_WRITE_FAILED ' + json.dumps(self.data), flush=True)
            raise NativeFailure('CHECKPOINT_WRITE_FAILED') from None

    def stage(self, value):
        self.data['stage'] = value
        self.save()

    def passed(self, check):
        self.data['checks'][check] = True
        self.save()

    def cleanup(self, name, action):
        # Evidence storage failure must never skip an authorized cleanup action.
        try:
            self.stage('cleanup_' + name)
        except NativeFailure:
            pass
        try:
            action()
        except BaseException as exc:
            self.data['cleanup_errors'].append(failure(exc, 'cleanup_' + name))
            self.data['status'] = 'failed'
        else:
            self.data['checks'][name] = True
        try:
            self.save()
        except NativeFailure:
            pass


def ps(script, expected=0, timeout=30):
    result = subprocess.run([str(powershell_path()), '-NoProfile', '-NonInteractive',
                             '-EncodedCommand', encoded(script)], capture_output=True, text=True, timeout=timeout)
    if result.returncode != expected:
        raise NativeFailure('POWERSHELL_UNEXPECTED_EXIT', result.returncode)
    return result

def quote(value):
    return "'" + str(value).replace("'", "''") + "'"

def require(value, code):
    if not value:
        raise NativeFailure(code)

def rule_query(name, store):
    # Filter in the native provider; enumerating every hosted-runner rule can time out.
    return "try { @(Get-NetFirewallRule -PolicyStore " + store + " -Name " + quote(name) + " -ErrorAction Stop) } catch { if ($_.FullyQualifiedErrorId -ne 'CmdletizationQuery_NotFound_Name,Get-NetFirewallRule') { throw } }"


def firewall_facts(name):
    script = "$rules=@(& {" + rule_query(name, "ActiveStore") + "}); $profiles=@(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop); $svc=Get-Service MpsSvc -ErrorAction Stop; $items=@($rules | ForEach-Object {$r=$_; $a=$r | Get-NetFirewallAddressFilter -ErrorAction Stop; $p=$r | Get-NetFirewallPortFilter -ErrorAction Stop; @{enabled=[string]$r.Enabled;direction=[string]$r.Direction;action=[string]$r.Action;profile=[string]$r.Profile;protocol=[string]$p.Protocol;remote_port=@($p.RemotePort);remote_address=@($a.RemoteAddress)}}); @{rules=$items;profiles=@($profiles | ForEach-Object {@{name=[string]$_.Name;enabled=[bool]$_.Enabled}});service=[string]$svc.Status} | ConvertTo-Json -Depth 5 -Compress"
    return json.loads(ps(script).stdout)


def primary_addresses():
    value = json.loads(ps("ConvertTo-Json -InputObject @([Net.Dns]::GetHostAddresses('ntp.aliyun.com') | ForEach-Object {$_.IPAddressToString}) -Compress").stdout)
    require(isinstance(value, list) and bool(value), 'PRIMARY_DNS_EMPTY')
    import ipaddress
    return sorted({str(ipaddress.ip_address(x)) for x in value})


def verify_block(evidence, name, addresses, stage):
    evidence.stage('verify_firewall_' + stage)
    facts = firewall_facts(name)
    evidence.data.setdefault('firewall_states', {})[stage] = facts
    evidence.save()
    require(facts['service'] == 'Running' and len(facts['profiles']) == 3 and
            all(x['enabled'] is True for x in facts['profiles']), 'FIREWALL_NOT_ACTIVE')
    require(len(facts['rules']) == 1, 'UNIQUE_FIREWALL_RULE_MISSING')
    rule = facts['rules'][0]
    require(rule['enabled'] == 'True' and rule['direction'] == 'Outbound' and
            rule['action'] == 'Block' and rule['profile'] == 'Any' and
            rule['protocol'] in ('UDP', '17') and rule['remote_port'] == ['123'], 'FIREWALL_SCOPE_MISMATCH')
    actual = sorted(x.removesuffix('/32').removesuffix('/128') for x in rule['remote_address'])
    require(actual == sorted(addresses), 'FIREWALL_ADDRESS_MISMATCH')


def cleanup_firewall(evidence, name):
    ps("$r=@(& {" + rule_query(name, "PersistentStore") + "}); $r | Remove-NetFirewallRule -ErrorAction Stop")
    facts = firewall_facts(name)
    evidence.data['firewall_after_cleanup'] = facts
    count = int(ps("@(& {" + rule_query(name, "PersistentStore") + "}).Count").stdout.strip())
    require(not facts['rules'] and count == 0, 'FIREWALL_CLEANUP_NOT_CONFIRMED')


def restore_timezone(evidence, zone):
    ps('Set-TimeZone -Id ' + quote(zone) + ' -ErrorAction Stop')
    actual = ps('(Get-TimeZone).Id').stdout.strip()
    evidence.data['timezone_after_cleanup'] = actual
    require(actual == zone, 'TIMEZONE_RESTORE_NOT_CONFIRMED')


VMIC_PATH = r'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\VMICTimeProvider'


def vmic_enabled():
    value = int(ps('(Get-ItemProperty -LiteralPath ' + quote(VMIC_PATH) + ' -Name Enabled -ErrorAction Stop).Enabled').stdout.strip())
    require(value in (0, 1), 'UNEXPECTED_VM_TIME_PROVIDER_VALUE')
    return value


def set_vmic(evidence, value, key):
    # This is a test precondition on a guarded disposable VM, never product policy.
    ps('Set-ItemProperty -LiteralPath ' + quote(VMIC_PATH) + ' -Name Enabled -Value ' + str(value) + ' -ErrorAction Stop; Restart-Service W32Time -ErrorAction Stop')
    actual = vmic_enabled()
    evidence.data[key] = actual
    require(actual == value, 'VM_TIME_PROVIDER_STATE_MISMATCH')


def synchronized_fallback(facts):
    # Hosted windows-2022 emits English w32tm output. Missing/unknown fields fail closed.
    import re
    def number(label):
        match = re.search(r'^' + re.escape(label) + r':\s*(\d+)', facts['status'], re.MULTILINE)
        return int(match.group(1)) if match else None
    leap, stratum, error = (number(x) for x in ('Leap Indicator', 'Stratum', 'Last Sync Error'))
    age = re.search(r'^Time since Last Good Sync Time:\s*([0-9.]+)s', facts['status'], re.MULTILINE)
    return ('time.windows.com' in facts['source'].lower() and leap == 0 and
            stratum is not None and 1 <= stratum <= 15 and error == 0 and
            age is not None and 0 <= float(age.group(1)) <= 30)


def observe_failed_source(evidence):
    # Read-only diagnostics AFTER failure. Observations never change the verdict.
    try:
        probe = ps(r"""$w=Join-Path ([Environment]::GetFolderPath('System')) 'w32tm.exe'; $text=(& $w /stripchart /computer:time.windows.com /dataonly /samples:2 | Out-String); @{exit_code=$LASTEXITCODE;output=$text} | ConvertTo-Json -Compress""", timeout=20)
        evidence.data['fallback_readonly_stripchart'] = json.loads(probe.stdout)
    except Exception as exc:
        evidence.data['fallback_readonly_stripchart'] = {'error': failure(exc, 'stripchart')}
    evidence.save()


def run():
    require(sys.platform == 'win32' and os.environ.get('GITHUB_ACTIONS') == 'true', 'GITHUB_WINDOWS_ONLY')
    require(os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'HOSTED_EPHEMERAL_RUNNER_ONLY')
    require(os.environ.get('GITHUB_REPOSITORY') == 'edwardzhang0304/chejin-ai-sales', 'REPOSITORY_MISMATCH')
    root = Path(os.environ['RUNNER_TEMP']) / 'clock-native-01026'
    root.mkdir(exist_ok=False)
    evidence = Evidence(root)
    evidence.data['reused_unchanged_lock_checks'] = {'run_id': 40819454895, 'scope': 'msvcrt/.NET lock and revocation; not rerun'}
    before_zone = None
    before_vmic = None
    ticket = None
    firewall = 'CheJinClockAcceptance-' + str(uuid.uuid4())
    try:
        evidence.data['product_files'] = {}
        for name in ['worker-client/chejin_worker_client/clock_sync_ticket.py',
                     'worker-client/chejin_worker_client/windows_clock_sync.py',
                     'worker-client/chejin_worker_client/shared_rules.py',
                     'worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/hook_time_rules.py']:
            original = subprocess.check_output(['git', 'show', PRODUCT + ':' + name], cwd=ROOT, timeout=15)
            actual = (ROOT / name).read_bytes()
            require(original == actual, 'PRODUCT_INPUT_MISMATCH')
            evidence.data['product_files'][name] = hashlib.sha256(actual).hexdigest()
        evidence.stage('isolate_vm_time_provider')
        before_vmic = vmic_enabled()
        evidence.data['vm_time_provider_before'] = before_vmic
        evidence.save()
        set_vmic(evidence, 0, 'vm_time_provider_during_test')
        evidence.stage('primary_unavailable_fallback')
        before_zone = ps('(Get-TimeZone).Id').stdout.strip()
        # Only an ephemeral GitHub VM: force the primary unreachable without editing product policy.
        addresses = primary_addresses()
        evidence.data['primary_addresses_before'] = addresses
        evidence.data['primary_coverage_basis'] = 'Exact product hostname; all resolved addresses blocked; DNS answers checked before and after helper. No packet capture claim; changed answers fail acceptance.'
        evidence.save()
        ps('New-NetFirewallRule -Name ' + quote(firewall) + ' -DisplayName ' + quote(firewall) + ' -Direction Outbound -Action Block -Enabled True -Profile Any -Protocol UDP -RemotePort 123 -RemoteAddress @(' + ','.join(quote(x) for x in addresses) + ') -ErrorAction Stop | Out-Null')
        verify_block(evidence, firewall, addresses, 'primary_only')
        ticket = ClockSyncTicket(root)
        op = 'clock-sync-' + str(uuid.uuid4())
        ticket.authorize(op)
        result = synchronize_windows(op, ticket.path)
        evidence.data['clock_sync'] = {k: result.get(k) for k in ('result', 'exit_code', 'resync_completed', 'helper_exit_confirmed', 'requested_peers')}
        evidence.save()
        require(result.get('result') == 'requested' and result.get('resync_completed') and result.get('helper_exit_confirmed'), 'FALLBACK_SYNC_NOT_CONFIRMED')
        evidence.stage('native_fallback_convergence')
        samples = []
        evidence.data['native_samples'] = samples
        observed_start = time.monotonic()
        deadline = observed_start + 30
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NativeFailure('FALLBACK_NOT_SYNCHRONIZED')
            facts = json.loads(ps(r"""$p=Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\Parameters'; $s=Get-CimInstance Win32_Service -Filter "Name='W32Time'"; $w=Join-Path ([Environment]::GetFolderPath('System')) 'w32tm.exe'; $source=(& $w /query /source | Out-String).Trim(); if($LASTEXITCODE -ne 0){throw 'source query failed'}; $status=(& $w /query /status /verbose | Out-String); if($LASTEXITCODE -ne 0){throw 'status query failed'}; @{peers=$p.NtpServer;startup=$s.StartMode;source=$source;status=$status;zone=(Get-TimeZone).Id} | ConvertTo-Json -Compress""", timeout=remaining).stdout)
            evidence.data['native_facts'] = facts
            samples.append({'elapsed_seconds': time.monotonic() - observed_start, 'facts': facts})
            evidence.save()
            require(facts['peers'] == result['requested_peers'], 'PEER_CONFIGURATION_MISMATCH')
            require(facts['startup'] == 'Auto' and facts['zone'] == 'China Standard Time', 'SERVICE_OR_ZONE_MISMATCH')
            if time.monotonic() > deadline:
                raise NativeFailure('FALLBACK_NOT_SYNCHRONIZED')
            if synchronized_fallback(facts):
                break
            if time.monotonic() - observed_start >= 30:
                raise NativeFailure('FALLBACK_NOT_SYNCHRONIZED')
            time.sleep(max(0, min(5, deadline - time.monotonic())))
        after_addresses = primary_addresses()
        evidence.data['primary_addresses_after'] = after_addresses
        evidence.save()
        require(after_addresses == addresses, 'PRIMARY_DNS_CHANGED_DURING_CHECK')
        verify_block(evidence, firewall, addresses, 'primary_after_helper')
        evidence.passed('primary_unavailable_native_fallback_selected')
        evidence.stage('all_sources_unavailable')
        ps('Set-NetFirewallRule -Name ' + quote(firewall) + ' -RemoteAddress Any -ErrorAction Stop')
        verify_block(evidence, firewall, ['Any'], 'all_sources')
        op2 = 'clock-sync-' + str(uuid.uuid4())
        ticket.authorize(op2)
        result2 = synchronize_windows(op2, ticket.path)
        evidence.data['all_unavailable'] = {k: result2.get(k) for k in ('result', 'exit_code', 'resync_completed', 'helper_exit_confirmed')}
        evidence.save()
        evidence.data['all_unavailable_native_status'] = ps(r"$w=Join-Path ([Environment]::GetFolderPath('System')) 'w32tm.exe'; & $w /query /status /verbose; exit $LASTEXITCODE").stdout
        evidence.save()
        require(result2.get('result') == 'resync_failed' and not result2.get('resync_completed') and result2.get('helper_exit_confirmed'), 'ALL_UNAVAILABLE_FALSE_SUCCESS')
        verify_block(evidence, firewall, ['Any'], 'all_sources_after_helper')
        evidence.passed('all_sources_unavailable_not_success')
    except BaseException as exc:
        evidence.data['primary_error'] = failure(exc, evidence.data['stage'])
        if isinstance(exc, NativeFailure) and exc.code == 'FALLBACK_NOT_SYNCHRONIZED':
            try:
                observe_failed_source(evidence)
            except Exception as diagnostic_error:
                evidence.data['post_failure_diagnostic_error'] = failure(diagnostic_error, 'post_failure_diagnostics')
    finally:
        evidence.cleanup('firewall_removed', lambda: cleanup_firewall(evidence, firewall))
        if ticket:
            evidence.cleanup('ticket_revoked', ticket.revoke)
        if before_vmic is not None:
            evidence.cleanup('vm_time_provider_restored', lambda: set_vmic(evidence, before_vmic, 'vm_time_provider_after_cleanup'))
        if before_zone is not None:
            evidence.cleanup('timezone_restored', lambda: restore_timezone(evidence, before_zone))
        failed = bool(evidence.data['primary_error'] or evidence.data['cleanup_errors'] or evidence.data['report_errors'])
        evidence.data['status'] = 'failed' if failed else 'passed'
        evidence.data['stage'] = 'complete'
        try:
            evidence.save()
        except NativeFailure:
            failed = True
        print(json.dumps(evidence.data), flush=True)
    return 1 if failed else 0

if __name__ == '__main__':
    sys.exit(run())
