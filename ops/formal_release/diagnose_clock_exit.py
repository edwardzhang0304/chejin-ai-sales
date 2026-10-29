"""Observe revoked-ticket exit propagation on an ephemeral Windows runner only."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from check_clock_native import ROOT, PRODUCT, Evidence, failure, require, ps
from check_clock_native import ClockSyncTicket, helper_script, synchronize_windows


def run():
    require(sys.platform == 'win32' and os.environ.get('GITHUB_ACTIONS') == 'true', 'GITHUB_WINDOWS_ONLY')
    require(os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'HOSTED_EPHEMERAL_RUNNER_ONLY')
    require(os.environ.get('GITHUB_REPOSITORY') == 'edwardzhang0304/chejin-ai-sales', 'REPOSITORY_MISMATCH')
    root = Path(os.environ['RUNNER_TEMP']) / 'clock-native-exit-01026'
    root.mkdir(exist_ok=False)
    evidence = Evidence(root)
    evidence.data['scope'] = 'Revoked ticket through direct helper and actual elevated wrapper. No valid clock-repair authorization; no NTP/fallback acceptance.'
    ticket = None
    try:
        evidence.data['product_files'] = {}
        for name in ['clock_sync_ticket.py', 'windows_clock_sync.py', 'shared_rules.py']:
            relative = 'worker-client/chejin_worker_client/' + name
            actual = (ROOT / relative).read_bytes()
            require(actual == subprocess.check_output(['git', 'show', PRODUCT + ':' + relative], cwd=ROOT, timeout=15), 'PRODUCT_INPUT_MISMATCH')
            evidence.data['product_files'][relative] = hashlib.sha256(actual).hexdigest()
        evidence.stage('revoked_exit_propagation')
        ticket = ClockSyncTicket(root)
        ticket.revoke()  # Never authorize this diagnostic operation to change the clock.
        operation = 'clock-sync-' + str(uuid.uuid4())
        before_zone = ps('(Get-TimeZone).Id').stdout.strip()
        direct = ps(helper_script(operation, ticket.path), expected=27)
        evidence.data['direct_helper_exit'] = direct.returncode
        evidence.save()
        wrapped = synchronize_windows(operation, ticket.path)
        evidence.data['elevated_wrapper_result'] = wrapped
        evidence.data['timezone_unchanged'] = ps('(Get-TimeZone).Id').stdout.strip() == before_zone
        evidence.save()
        require(evidence.data['timezone_unchanged'], 'REVOKED_HELPER_CHANGED_ZONE')
        require(wrapped.get('result') == 'revoked' and wrapped.get('exit_code') == 27
                and wrapped.get('helper_exit_confirmed') and not wrapped.get('resync_completed'), 'ELEVATED_WRAPPER_EXIT_MISMATCH')
        evidence.passed('revoked_exit_propagated_by_actual_wrapper')
    except BaseException as exc:
        evidence.data['primary_error'] = failure(exc, evidence.data['stage'])
    finally:
        if ticket:
            evidence.cleanup('ticket_revoked', ticket.revoke)
        failed = bool(evidence.data['primary_error'] or evidence.data['cleanup_errors'] or evidence.data['report_errors'])
        evidence.data['status'] = 'failed' if failed else 'diagnostic_passed_not_release_acceptance'
        evidence.data['stage'] = 'complete'
        try:
            evidence.save()
        except Exception:
            failed = True
        print(json.dumps(evidence.data), flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(run())
