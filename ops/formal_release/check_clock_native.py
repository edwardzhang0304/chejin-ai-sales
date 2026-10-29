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
PRODUCT = '56137b906456878913f5f3f4274fc8aca73366e4'

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

def run():
    require(sys.platform == 'win32' and os.environ.get('GITHUB_ACTIONS') == 'true', 'GITHUB_WINDOWS_ONLY')
    require(os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'HOSTED_EPHEMERAL_RUNNER_ONLY')
    require(os.environ.get('GITHUB_REPOSITORY') == 'edwardzhang0304/chejin-ai-sales', 'REPOSITORY_MISMATCH')
    root = Path(os.environ['RUNNER_TEMP']) / 'clock-native-01025'
    root.mkdir(exist_ok=False)
    evidence = Evidence(root)
    ticket = None
    before_zone = None
    try:
        evidence.data['product_files'] = {}
        for name in ['worker-client/chejin_worker_client/clock_sync_ticket.py',
                     'worker-client/chejin_worker_client/windows_clock_sync.py']:
            original = subprocess.check_output(['git', 'show', PRODUCT + ':' + name], cwd=ROOT, timeout=15)
            actual = (ROOT / name).read_bytes()
            require(original == actual, 'PRODUCT_INPUT_MISMATCH')
            evidence.data['product_files'][name] = hashlib.sha256(actual).hexdigest()
        evidence.stage('msvcrt_excludes_dotnet')
        ticket = ClockSyncTicket(root)
        op = 'clock-sync-' + str(uuid.uuid4())
        ticket.authorize(op)
        dotnet = "$f=[IO.File]::Open(" + quote(ticket.path) + ",'Open','ReadWrite','ReadWrite');try{$f.Lock(0,1);$f.Unlock(0,1);exit 0}catch{exit 41}finally{$f.Dispose()}"
        with ticket.locked():
            ps(dotnet, expected=41)
        ps(dotnet)
        evidence.passed('msvcrt_excludes_dotnet')
        evidence.stage('dotnet_excludes_msvcrt')
        ready, release = root / 'dotnet-ready', root / 'dotnet-release'
        holder = "$f=[IO.File]::Open(" + quote(ticket.path) + ",'Open','ReadWrite','ReadWrite');try{$f.Lock(0,1);[IO.File]::WriteAllText(" + quote(ready) + ",'ready');$until=[DateTime]::UtcNow.AddSeconds(30);while(!(Test-Path -LiteralPath " + quote(release) + ")){if([DateTime]::UtcNow -gt $until){throw 'bounded holder timeout'};Start-Sleep -Milliseconds 50};$f.Unlock(0,1)}finally{$f.Dispose()}"
        proc = subprocess.Popen([str(powershell_path()), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded(holder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        holder_error = None
        try:
            until = time.monotonic() + 15
            while not ready.exists() and time.monotonic() < until:
                require(proc.poll() is None, 'HOLDER_EXITED_BEFORE_READY')
                time.sleep(.05)
            require(ready.exists(), 'HOLDER_READY_TIMEOUT')
            try:
                ticket.revoke()
            except BlockingIOError:
                pass
            else:
                raise NativeFailure('DOTNET_LOCK_DID_NOT_EXCLUDE_MSVCRT')
        except BaseException as exc:
            holder_error = exc
            evidence.data['primary_error'] = failure(exc, evidence.data['stage'])
        finally:
            def close_holder():
                release.write_text('release')
                rc = proc.wait(timeout=35)
                if rc != 0:
                    raise NativeFailure('HOLDER_EXIT_FAILED', rc)
            evidence.cleanup('holder_released', close_holder)
        if holder_error:
            raise holder_error
        require(not evidence.data['cleanup_errors'], 'HOLDER_CLEANUP_FAILED')
        ticket.revoke()
        evidence.passed('dotnet_excludes_msvcrt_and_releases')
        evidence.stage('revoked_helper')
        before_zone = ps('(Get-TimeZone).Id').stdout.strip()
        ps(helper_script(op, ticket.path), expected=27)
        require(ps('(Get-TimeZone).Id').stdout.strip() == before_zone, 'REVOKED_HELPER_CHANGED_ZONE')
        evidence.passed('revoked_helper_no_timezone_change')
        evidence.stage('prior_attempt_rejected')
        new_op = 'clock-sync-' + str(uuid.uuid4())
        ticket.authorize(new_op)
        ps(helper_script(op, ticket.path), expected=27)
        evidence.passed('prior_attempt_rejected')
        evidence.stage('actual_ntp_and_timezone')
        result = synchronize_windows(new_op, ticket.path)
        evidence.data['clock_sync'] = {k: result.get(k) for k in ('result', 'exit_code', 'resync_completed', 'helper_exit_confirmed')}
        evidence.save()
        if result.get('result') != 'requested':
            raise NativeFailure('ACTUAL_NTP_NOT_CONFIRMED', result.get('exit_code'))
        require(result.get('resync_completed') and result.get('helper_exit_confirmed'), 'HELPER_RESULT_UNCONFIRMED')
        require(ps('(Get-TimeZone).Id').stdout.strip() == 'China Standard Time', 'TIMEZONE_NOT_CONFIRMED')
        evidence.passed('actual_ntp_and_timezone')
    except BaseException as exc:
        if evidence.data['primary_error'] is None:
            evidence.data['primary_error'] = failure(exc, evidence.data['stage'])
        evidence.data['status'] = 'failed'
    finally:
        if ticket:
            evidence.cleanup('ticket_revoked', ticket.revoke)
        if before_zone is not None:
            def restore_zone():
                ps('Set-TimeZone -Id ' + quote(before_zone) + ' -ErrorAction Stop')
                require(ps('(Get-TimeZone).Id').stdout.strip() == before_zone, 'TIMEZONE_RESTORE_NOT_CONFIRMED')
            evidence.cleanup('timezone_restored', restore_zone)
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
