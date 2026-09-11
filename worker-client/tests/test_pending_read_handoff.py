"""Real SQLite and signed-package updater checks, with synthetic pending facts.

Full original .75 facts are covered by the backend cross-version process test.
These focused tests cover installation boundaries; stub executables do not
stand in for Windows health/EXE acceptance.
"""
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import shutil

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from chejin_worker_client import storage
from chejin_worker_client.models import Binding, ClientRelease
from chejin_worker_client.pending_read_recovery import inspect_pending_read, package_recovery_capability, accepts_handoff
from chejin_worker_client.update_data_snapshot import capture_data_baseline, protected_update_snapshot, assert_protected_update_snapshot
from chejin_worker_client.update_data_access import acquire_update_access, clear_update_writer
from chejin_worker_client.chejin_updater import validate_update_plan, run_update
from chejin_worker_client.client_update import canonical_release_manifest, ClientUpdateError
from test_chejin_updater import _prepare_plan, HEALTHY_WORKER, FAILED_WORKER


@pytest.fixture(autouse=True)
def reap_only_this_tests_stub_workers(monkeypatch):
    from chejin_worker_client import chejin_updater as updater
    start = updater._start_worker
    children = []
    def tracked_start(*args, **kwargs):
        process = start(*args, **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(updater, '_start_worker', tracked_start)
    yield
    # Stub workers exit naturally after two seconds. Reap them before the next
    # coordinator test inventories this pytest process's child writers.
    for process in children:
        process.wait(timeout=5)


def pending(data, monkeypatch):
    monkeypatch.setattr(storage, 'APP_DIR', data)
    monkeypatch.setattr(storage, 'DB_FILE', data / 'worker_client.sqlite3')
    storage.save_binding(Binding('worker-A', 'synthetic-test-token', 'instance-A', run_status='faulted'))
    storage.begin_runtime_flow('read-A', 'c2_read')
    storage.save_c2_state('inflight_finish_receipt:read-A', {
        'terminal_kind': 'technical_failed', 'conversation_id': 'customer-A',
        'error_code': 'MESSAGE_OBSERVATION_MAPPING_INCOMPLETE:FACT_SETTLEMENT_REQUIRED'})
    pair = package_recovery_capability()['contracts'][0]
    body = {'contract_version': 3, 'contract_revision': pair['revision'], 'contract_sha256': pair['sha256'],
            'read_run_id': 'read-A', 'conversation_id': 'customer-A', 'authorization_revision': 'revision-A',
            'messages': [{'source_message_key': 'source-A', 'dedupe_key': 'dedupe-A',
                          'message_type': 'text', 'sender_role_hint': 'customer', 'content': 'Synthetic question', 'item_state': 'completed'}]}
    storage.save_c2_ledger_terminal(conversation_id='customer-A', source_message_key='source-A',
        origin_read_run_id='read-A', dedupe_key='dedupe-A', message_type='text', terminal_state='completed', ingest_state='waiting')
    storage.enqueue_c2_outbox(body)
    return inspect_pending_read(data)


@pytest.mark.parametrize('corruption', ['other_customer', 'other_flow', 'missing_fact', 'bad_payload', 'physical_journal', 'running', 'unfinished_action', 'pending_send'])
def test_handoff_refuses_unproven_or_physical_data(tmp_path, monkeypatch, corruption):
    data = tmp_path / 'data'
    before = pending(data, monkeypatch)
    assert accepts_handoff(package_recovery_capability(), before)
    with storage.db_connection() as db:
        if corruption == 'other_customer': db.execute("UPDATE c2_ingest_outbox SET conversation_id='customer-B'")
        if corruption == 'other_flow': db.execute("UPDATE c2_message_ledger SET origin_read_run_id='read-B'")
        if corruption == 'missing_fact': db.execute("UPDATE c2_message_ledger SET source_message_key='missing'")
        if corruption == 'bad_payload': db.execute("UPDATE c2_ingest_outbox SET payload_json='{}'")
        if corruption == 'running': db.execute("UPDATE binding SET run_status='running'")
        if corruption == 'unfinished_action':
            db.execute("INSERT INTO c2_action_journal VALUES ('read-A','customer-A','source-A','read-A','{}','now','now')")
        if corruption == 'pending_send':
            db.execute("INSERT INTO reply_send_ack_outbox(reply_action_id,task_id,send_token,status,created_at,updated_at) VALUES ('reply-A','task-A','synthetic','waiting','now','now')")
        db.commit()
    if corruption == 'physical_journal':
        path = data / 'transactions/actions/voice/pending.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}')
    with pytest.raises(RuntimeError, match='UPDATE_PENDING_READ_NOT_TRANSFERABLE'):
        inspect_pending_read(data)


def signed_handoff_plan(tmp_path, monkeypatch, *, source=HEALTHY_WORKER, capable=True):
    path, token, current, previous = _prepare_plan(tmp_path, monkeypatch, new_worker=source)
    plan = json.loads(path.read_text())
    data = Path(plan['data_dir'])
    handoff = pending(data, monkeypatch)
    boundary = plan['safe_boundary']
    boundary.update(confirmed_run_status='faulted', inflight_flow_id='read-A',
                    pending_c2_outbox=1, waiting_ledger=1, settlement_complete=False, pending_read_handoff=handoff)
    manifest_path = Path(plan['staged_program_dir']) / 'update-package-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if capable:
        manifest['pending_read_recovery'] = package_recovery_capability()
    manifest_path.write_text(json.dumps(manifest))
    key = Ed25519PrivateKey.generate()
    keys = json.loads((path.parent / 'keys.json').read_text())
    keys['keys'][0]['public_key_base64'] = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    (path.parent / 'keys.json').write_text(json.dumps(keys))
    release = ClientRelease(**{**plan['release'], 'package_manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest()})
    plan['release'] = {**release.__dict__, 'manifest_signature': base64.b64encode(key.sign(canonical_release_manifest(release))).decode()}
    path.write_text(json.dumps(plan))
    return plan, path, token, data, handoff


@pytest.mark.parametrize('failure', [False, True])
def test_program_switch_or_rollback_preserves_pending_data(tmp_path, monkeypatch, failure):
    plan, path, token, data, handoff = signed_handoff_plan(tmp_path, monkeypatch, source=FAILED_WORKER if failure else HEALTHY_WORKER)
    before = protected_update_snapshot(data_dir=data, digest_key=token)
    assert run_update(path, token) == (1 if failure else 0)
    result = json.loads((path.parent / 'update-result.json').read_text())
    assert result['result_code'] == ('UPDATE_ROLLED_BACK' if failure else 'UPDATE_SUCCEEDED')
    assert_protected_update_snapshot(before, data_dir=data, digest_key=token)
    assert inspect_pending_read(data) == handoff
    assert storage.load_binding().run_status == 'faulted'
    assert storage.load_runtime_control()['inflight_flow_id'] == 'read-A'


def test_pending_facts_change_after_handoff_prevents_baseline_and_replacement(tmp_path, monkeypatch):
    plan, path, token, data, _ = signed_handoff_plan(tmp_path, monkeypatch)
    with storage.db_connection() as db:
        body = json.loads(db.execute('SELECT payload_json FROM c2_ingest_outbox').fetchone()[0])
        body['messages'][0]['content'] = 'changed fact'
        db.execute('UPDATE c2_ingest_outbox SET payload_json=?', (json.dumps(body),)); db.commit()
    with acquire_update_access(plan, token):
        with pytest.raises(RuntimeError, match='UPDATE_PENDING_READ_HANDOFF_CHANGED'):
            capture_data_baseline(plan, path, token)
    assert not Path(plan['data_baseline_path']).exists()


def test_target_must_sign_recovery_capability_and_preserve_action_barriers(tmp_path, monkeypatch):
    plan, path, token, data, _ = signed_handoff_plan(tmp_path, monkeypatch)
    validate_update_plan(path, token)
    for key in ('pending_sent_ack', 'pending_file_action_journal', 'pending_sqlite_action_journal', 'task_lease_active', 'ui_lock_active', 'sidecar_active'):
        plan['safe_boundary'][key] = 1
        path.write_text(json.dumps(plan))
        with pytest.raises(ClientUpdateError):
            validate_update_plan(path, token)
        plan['safe_boundary'][key] = 0
    path.write_text(json.dumps(plan))
    manifest_path = Path(plan['staged_program_dir']) / 'update-package-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest.pop('pending_read_recovery')
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ClientUpdateError):
        validate_update_plan(path, token)


def test_valid_signature_does_not_make_an_unsupported_target_compatible(tmp_path, monkeypatch):
    plan, path, token, _, _ = signed_handoff_plan(tmp_path, monkeypatch, capable=False)
    with pytest.raises(ClientUpdateError) as error:
        validate_update_plan(path, token)
    assert error.value.code == 'UPDATE_PACKAGE_INCOMPATIBLE'


@pytest.mark.parametrize('case', ['valid', 'initialization_changes_fact', 'unsupported_runtime'])
def test_new_worker_initialization_then_real_baseline_and_handoff_validation(tmp_path, monkeypatch, case):
    from chejin_worker_client import post_update_health as health
    from chejin_worker_client import pending_read_recovery as recovery
    plan, path, token, data, handoff = signed_handoff_plan(tmp_path, monkeypatch)
    # Isolated installed-executable identity; real DB initialization/checks below.
    plan['current_program_dir'] = plan['staged_program_dir']
    path.write_text(json.dumps(plan))
    monkeypatch.setattr(health, 'CONFIG', SimpleNamespace(app_dir=data))
    monkeypatch.setattr(health, '__version__', plan['target_version'])
    monkeypatch.setattr(health.sys, 'executable', str(Path(plan['current_program_dir']) / 'CheJinWorkerClient.exe'))
    initialize = storage.init_db
    def bad_initialization(db):
        initialize(db)
        db.execute("UPDATE c2_ingest_outbox SET payload_json='{}'")
        db.commit()
    if case == 'initialization_changes_fact': monkeypatch.setattr(storage, 'init_db', bad_initialization)
    if case == 'unsupported_runtime': monkeypatch.setattr(recovery, 'package_recovery_capability', lambda: {'protocol_version': 1, 'contracts': []})
    try:
        with acquire_update_access(plan, token):
            baseline = capture_data_baseline(plan, path, token)
            assert baseline['pending_read_handoff'] == handoff
            if case == 'valid':
                health.verify_post_update_startup(path, token)
                assert inspect_pending_read(data) == handoff
            else:
                with pytest.raises(RuntimeError, match='UPDATE_PROTECTED_DATABASE_CHANGED|UPDATE_PENDING_READ_HANDOFF_INVALID'):
                    health.verify_post_update_startup(path, token)
            assert not Path(plan['healthy_marker_path']).exists()
    finally:
        clear_update_writer()


@pytest.mark.parametrize('case', ['complete', 'missing', 'corrupt'])
def test_manifest_capability_requires_actual_packaged_contracts(tmp_path, case):
    root = Path(__file__).resolve().parents[1]
    package = tmp_path / 'package'; package.mkdir()
    for name in ('CheJinWorkerClient.exe', 'CheJinUpdater.exe'):
        (package / name).write_bytes(b'synthetic executable; manifest test only')
    shutil.copytree(root.parent / 'contracts', package / '_internal/contracts')
    legacy = package / '_internal/contracts/recovery/c2_contract_v3_0.9.75.json'
    if case == 'missing': legacy.unlink()
    if case == 'corrupt': legacy.write_text('{}')
    output = package / 'update-package-manifest.json'
    from chejin_worker_client import __version__
    result = subprocess.run([sys.executable, str(root / 'scripts/generate-update-package-manifest.py'),
                             '--package-root', str(package), '--version', __version__, '--git-commit', 'a' * 40,
                             '--output', str(output)], capture_output=True, text=True, timeout=10)
    if case == 'complete':
        assert result.returncode == 0, result.stderr
        assert json.loads(output.read_text())['pending_read_recovery'] == package_recovery_capability()
    else:
        assert result.returncode != 0 and not output.exists()
        assert 'packaged recovery contract ' + ('missing' if case == 'missing' else 'mismatch') in result.stderr


@pytest.mark.parametrize('status,expected', [('running', 'paused'), ('paused', 'paused'), ('faulted', 'faulted')])
def test_contract_rejection_never_downgrades_a_persisted_fault(tmp_path, monkeypatch, status, expected):
    from chejin_worker_client.task_runner import TaskRunner
    data = tmp_path / 'data'
    pending(data, monkeypatch)
    binding = storage.load_binding(); binding.run_status = status; storage.save_binding(binding)
    runner = object.__new__(TaskRunner); runner.binding = binding
    runner._pause_for_permanent_outbox_contract_error(binding)
    assert runner.binding.run_status == storage.load_binding().run_status == expected
    assert runner._pending_run_status_sync == expected
    assert storage.load_runtime_control()['pause_requested']
