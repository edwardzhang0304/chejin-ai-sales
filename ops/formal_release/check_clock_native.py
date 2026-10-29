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
PRODUCT = '38118627933b917cc35e988937ede1cd7c23b849'
BLOCKED_HOSTS = ('ntp.aliyun.com', 'time.windows.com')
FALLBACK_HOST = 'time.cloudflare.com'
# OS source selection is not ClockSync's 30s backend-comparison UI window.
# Run40869643554 observed native fallback only after about278s, without resync.
NATIVE_SELECTION_SECONDS = 360

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
            self.data['cleanup_errors'].append(failure(exc, self.data['stage']))
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

def firewall_facts(name):
    # COM reads the effective local policy without slow CIM filter associations.
    # A single active profile is required: COM automation can hide S_FALSE
    # (partial applicability across profiles). Multi-profile fixtures fail closed.
    # The exact persistent key is checked too.
    script = r"""$ErrorActionPreference='Stop'; $fw=New-Object -ComObject HNetCfg.FwPolicy2; $name=__NAME__; $items=@($fw.Rules | Where-Object {$_.Name -eq $name} | ForEach-Object {@{enabled=[bool]$_.Enabled;direction=[int]$_.Direction;action=[int]$_.Action;profile=[int]$_.Profiles;protocol=[int]$_.Protocol;remote_port=[string]$_.RemotePorts;remote_address=@(([string]$_.RemoteAddresses).Split(','))}}); $key=Get-Item -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules' -ErrorAction Stop; @{rules=$items;profiles=@(1,2,4 | ForEach-Object {@{id=$_;enabled=[bool]$fw.FirewallEnabled($_)}});service=[string](Get-Service MpsSvc -ErrorAction Stop).Status;active_profile_types=[int]$fw.CurrentProfileTypes;local_policy_modify_state=[int]$fw.LocalPolicyModifyState;persistent_rule_exists=($null -ne $key.GetValue($name,$null))} | ConvertTo-Json -Depth 5 -Compress""".replace('__NAME__', quote(name))
    return json.loads(ps(script).stdout)


def blocked_addresses():
    import ipaddress
    result = {}
    for host in BLOCKED_HOSTS:
        value = json.loads(ps("ConvertTo-Json -InputObject @([Net.Dns]::GetHostAddresses(" + quote(host) + ") | ForEach-Object {$_.IPAddressToString}) -Compress").stdout)
        require(isinstance(value, list) and bool(value), 'BLOCKED_SOURCE_DNS_EMPTY')
        result[host] = sorted({str(ipaddress.ip_address(x)) for x in value})
    return result


def address_union(by_host):
    return sorted({address for values in by_host.values() for address in values})


def single_firewall_address(value):
    import ipaddress
    if value == '*':
        return 'Any'
    if '-' in value:
        first, last = value.split('-', 1)
        start, end = ipaddress.ip_address(first), ipaddress.ip_address(last)
        require(start == end, 'NON_SINGLETON_FIREWALL_RANGE')
        return str(start)
    if '/' in value:
        network = ipaddress.ip_network(value, strict=False)
        require(network.num_addresses == 1, 'NON_SINGLETON_FIREWALL_NETWORK')
        return str(network.network_address)
    return str(ipaddress.ip_address(value))


def verify_block(evidence, name, addresses, stage):
    evidence.stage('verify_firewall_' + stage)
    facts = firewall_facts(name)
    evidence.data.setdefault('firewall_states', {})[stage] = facts
    evidence.save()
    require(facts['service'] == 'Running' and len(facts['profiles']) == 3 and
            all(x['enabled'] is True for x in facts['profiles']) and
            facts['active_profile_types'] in (1, 2, 4) and
            facts['local_policy_modify_state'] == 0 and facts['persistent_rule_exists'] is True, 'FIREWALL_NOT_ACTIVE')
    require(len(facts['rules']) == 1, 'UNIQUE_FIREWALL_RULE_MISSING')
    rule = facts['rules'][0]
    require(rule['enabled'] is True and rule['direction'] == 2 and
            rule['action'] == 0 and rule['profile'] == 2147483647 and
            rule['protocol'] == 17 and rule['remote_port'] == '123', 'FIREWALL_SCOPE_MISMATCH')
    actual = sorted(single_firewall_address(x) for x in rule['remote_address'])
    require(actual == sorted(addresses), 'FIREWALL_ADDRESS_MISMATCH')


def cleanup_firewall(evidence, name):
    evidence.data['stage'] = 'cleanup_firewall_remove_rule'
    ps("$fw=New-Object -ComObject HNetCfg.FwPolicy2; $fw.Rules.Remove(" + quote(name) + ")")
    evidence.data['firewall_remove_command_completed'] = True
    evidence.data['stage'] = 'cleanup_firewall_read_active'
    facts = firewall_facts(name)
    evidence.data['firewall_after_cleanup'] = facts
    require(not facts['rules'] and facts['persistent_rule_exists'] is False, 'FIREWALL_CLEANUP_NOT_CONFIRMED')


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


def native_facts(timeout):
    return json.loads(ps(r"""$p=Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\Parameters'; $s=Get-CimInstance Win32_Service -Filter "Name='W32Time'"; $w=Join-Path ([Environment]::GetFolderPath('System')) 'w32tm.exe'; $source=(& $w /query /source | Out-String).Trim(); if($LASTEXITCODE -ne 0){throw 'source query failed'}; $status=(& $w /query /status /verbose | Out-String); if($LASTEXITCODE -ne 0){throw 'status query failed'}; @{peers=$p.NtpServer;startup=$s.StartMode;source=$source;status=$status;zone=(Get-TimeZone).Id} | ConvertTo-Json -Compress""", timeout=timeout).stdout)


def synchronized_fallback(facts):
    # Hosted windows-2022 emits English w32tm output. Missing/unknown fields fail closed.
    import re
    def number(label):
        match = re.search(r'^' + re.escape(label) + r':\s*(\d+)', facts['status'], re.MULTILINE)
        return int(match.group(1)) if match else None
    leap, stratum, error = (number(x) for x in ('Leap Indicator', 'Stratum', 'Last Sync Error'))
    age = re.search(r'^Time since Last Good Sync Time:\s*([0-9.]+)s', facts['status'], re.MULTILINE)
    return (facts['source'].lower().split(',', 1)[0].strip() == FALLBACK_HOST and leap == 0 and
            stratum is not None and 1 <= stratum <= 15 and error == 0 and
            age is not None and 0 <= float(age.group(1)) <= 30)


def observe_native_details(evidence, label):
    """Read only on disposable CI; a diagnostic error never changes acceptance."""
    observations = {}
    evidence.data.setdefault('native_details', {})[label] = observations
    commands = {
        'configuration': r"$w=Join-Path ([Environment]::GetFolderPath('System')) 'w32tm.exe'; & $w /query /configuration /verbose; exit $LASTEXITCODE",
        'peers': r"$w=Join-Path ([Environment]::GetFolderPath('System')) 'w32tm.exe'; & $w /query /peers /verbose; exit $LASTEXITCODE",
        'time_service_events': r"$events=@(); try {$events=@(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Time-Service';StartTime=(Get-Date).AddMinutes(-10)} -MaxEvents 30 -ErrorAction Stop)} catch {if ($_.FullyQualifiedErrorId -ne 'NoMatchingEventsFound,Microsoft.PowerShell.Commands.GetWinEventCommand') {throw}}; ConvertTo-Json -InputObject @($events | ForEach-Object {@{id=$_.Id;time=$_.TimeCreated.ToUniversalTime().ToString('o');message=$_.Message}}) -Depth 4 -Compress",
    }
    for name, command in commands.items():
        try:
            result = ps(command, timeout=15)
            observations[name] = {'output': result.stdout[:32768], 'truncated': len(result.stdout) > 32768}
        except Exception as exc:
            observations[name] = {'error': failure(exc, label + '_' + name)}
        evidence.save()


def observe_failed_source(evidence):
    # Read-only diagnostics AFTER failure. Observations never change the verdict.
    observe_native_details(evidence, 'failed_fallback')
    try:
        probe = ps(r"""$w=Join-Path ([Environment]::GetFolderPath('System')) 'w32tm.exe'; $text=(& $w /stripchart /computer:time.cloudflare.com /dataonly /samples:2 | Out-String); @{exit_code=$LASTEXITCODE;output=$text} | ConvertTo-Json -Compress""", timeout=20)
        evidence.data['fallback_readonly_stripchart'] = json.loads(probe.stdout)
    except Exception as exc:
        evidence.data['fallback_readonly_stripchart'] = {'error': failure(exc, 'stripchart')}
    evidence.save()


def run(case="full"):
    require(case in ("full", "outage-only"), "UNKNOWN_NATIVE_CASE")
    require(sys.platform == 'win32' and os.environ.get('GITHUB_ACTIONS') == 'true', 'GITHUB_WINDOWS_ONLY')
    require(os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'HOSTED_EPHEMERAL_RUNNER_ONLY')
    require(os.environ.get('GITHUB_REPOSITORY') == 'edwardzhang0304/chejin-ai-sales', 'REPOSITORY_MISMATCH')
    root = Path(os.environ['RUNNER_TEMP']) / 'clock-native-01026'
    root.mkdir(exist_ok=False)
    evidence = Evidence(root)
    evidence.data['case'] = case
    evidence.data['scope'] = 'all-source outage only; Cloudflare selection requires separate evidence' if case == 'outage-only' else 'fallback and outage'
    evidence.data['reused_unchanged_lock_checks'] = {'run_id': 40819454895, 'scope': 'msvcrt/.NET lock and revocation; not rerun'}
    before_zone = None
    before_vmic = None
    ticket = None
    firewall = 'CheJinClockAcceptance-' + str(uuid.uuid4())
    try:
        evidence.data['product_files'] = {}
        for name in ['worker-client/chejin_worker_client/clock_sync_ticket.py',
                     'worker-client/chejin_worker_client/windows_clock_sync.py',
                     'worker-client/chejin_worker_client/clock_sync.py',
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
        before_zone = ps('(Get-TimeZone).Id').stdout.strip()
        ticket = ClockSyncTicket(root)
        if case == 'full':
            evidence.stage('two_sources_unavailable_cloudflare')
            # Only an ephemeral GitHub VM: force the first two sources unreachable without editing product policy.
            by_host = blocked_addresses()
            addresses = address_union(by_host)
            evidence.data['blocked_source_addresses_before'] = by_host
            evidence.data['blocked_coverage_basis'] = 'Both preceding product hostnames; all resolved addresses blocked; DNS answers checked before and after helper. No packet capture claim; changed answers fail acceptance.'
            evidence.save()
            ps('New-NetFirewallRule -Name ' + quote(firewall) + ' -DisplayName ' + quote(firewall) + ' -Direction Outbound -Action Block -Enabled True -Profile Any -Protocol UDP -RemotePort 123 -RemoteAddress @(' + ','.join(quote(x) for x in addresses) + ') -ErrorAction Stop | Out-Null')
            verify_block(evidence, firewall, addresses, 'two_sources')
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
            deadline = observed_start + NATIVE_SELECTION_SECONDS
            evidence.data['verification_scope'] = {
                'native_selection_max_seconds': NATIVE_SELECTION_SECONDS,
                'client_confirmation_seconds': 30,
                'client_confirmation_basis': 'Unchanged ClockSync._verify requires fresh backend comparison and reference, not native source text. Existing reviewed unconfirmed/pause controls reused.',
                'previous_failed_runs_preserved': [40867497328, 40868625711, 40869643554],
            }
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NativeFailure('FALLBACK_NOT_SYNCHRONIZED')
                facts = native_facts(remaining)
                evidence.data['native_facts'] = facts
                samples.append({'elapsed_seconds': time.monotonic() - observed_start, 'facts': facts})
                evidence.save()
                require(facts['peers'] == result['requested_peers'], 'PEER_CONFIGURATION_MISMATCH')
                require(facts['startup'] == 'Auto' and facts['zone'] == 'China Standard Time', 'SERVICE_OR_ZONE_MISMATCH')
                if time.monotonic() > deadline:
                    raise NativeFailure('FALLBACK_NOT_SYNCHRONIZED')
                if synchronized_fallback(facts):
                    evidence.data['native_convergence_seconds'] = time.monotonic() - observed_start
                    evidence.save()
                    break
                if time.monotonic() - observed_start >= NATIVE_SELECTION_SECONDS:
                    raise NativeFailure('FALLBACK_NOT_SYNCHRONIZED')
                time.sleep(max(0, min(5, deadline - time.monotonic())))
            after_by_host = blocked_addresses()
            evidence.data['blocked_source_addresses_after'] = after_by_host
            evidence.save()
            require(after_by_host == by_host, 'BLOCKED_SOURCE_DNS_CHANGED_DURING_CHECK')
            verify_block(evidence, firewall, addresses, 'two_sources_after_helper')
            evidence.passed('two_sources_unavailable_native_cloudflare_selected')
            evidence.stage('all_sources_unavailable')
            ps("$ErrorActionPreference='Stop'; $fw=New-Object -ComObject HNetCfg.FwPolicy2; $rule=$fw.Rules.Item(" + quote(firewall) + "); $rule.RemoteAddresses='*'")
        else:
            evidence.stage('create_all_sources_block')
            ps('New-NetFirewallRule -Name ' + quote(firewall) + ' -DisplayName ' + quote(firewall) + ' -Direction Outbound -Action Block -Enabled True -Profile Any -Protocol UDP -RemotePort 123 -RemoteAddress Any -ErrorAction Stop | Out-Null')
        evidence.stage('all_sources_unavailable')
        verify_block(evidence, firewall, ['Any'], 'all_sources')
        # A fresh W32Time session prevents the successful prior case's cached
        # samples from masquerading as newly available time during an outage.
        ps('Restart-Service W32Time -ErrorAction Stop')
        evidence.data['all_unavailable_initial_facts'] = native_facts(15)
        evidence.save()
        require(not synchronized_fallback(evidence.data['all_unavailable_initial_facts']), 'OUTAGE_FIXTURE_RETAINED_FALLBACK')
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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=['full', 'outage-only'], default='full')
    sys.exit(run(parser.parse_args().case))
