"""Original selected EXE + original pending data -> exact candidate EXE recovery.

Only synthetic isolated data, loopback HTTP and a controlled model. Fixture
creation uses the complete original source; installation/recovery uses untouched
signed EXEs. This does not attest physical WeChat sending or production recovery.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("upgrade_gate", Path(__file__).with_name("run-windows-client-upgrade-test.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def fixture_operation(operation, cfg):
    # Called only in a child whose DB is under this run's fresh temporary folder.
    sys.path[:0] = [str(Path(cfg['old_source']) / 'backend'), str(Path(cfg['old_source']) / 'backend/tests')]
    from app.core.database import Base, engine, SessionLocal
    assert engine.url.host == '127.0.0.1' and engine.url.database.startswith('chejin_pending_read_test')
    assert engine.dialect.name == 'postgresql'
    from app.models.base import utcnow
    from app.models.sales import Sales
    from app.models.c3 import Conversation, HandoffEvent
    from app.models.wechat import WechatSessionBinding
    from sqlalchemy import select
    if operation == 'create':
        from app.models.worker import Worker
        from app.models.lead import Lead
        from app.services.worker_token_service import hash_worker_token, encrypt_worker_token
        from sqlalchemy import inspect
        from sqlalchemy.schema import CreateSchema
        assert not inspect(engine).get_table_names(), 'Fixture database must be empty'
        with engine.begin() as connection:
            for schema in {table.schema for table in Base.metadata.tables.values() if table.schema}:
                connection.execute(CreateSchema(schema, if_not_exists=True))
        Base.metadata.create_all(engine)
        with SessionLocal() as db:
            token = 'synthetic-pending-read-loopback-token'
            owner = Worker(worker_name='Synthetic Windows fixture', enabled=True, run_status='running',
                online_status='online', rpa_component_status='ready', wechat_status='logged_in',
                last_heartbeat_at=utcnow(), client_binding_state='bound', client_instance_id='followup-test',
                bound_at=utcnow(), worker_token_hash=hash_worker_token(token), worker_token_encrypted=encrypt_worker_token(token))
            db.add(owner); db.flush()
            worker = {'id': owner.id, 'worker_token': token}
            rows = []
            for index in range(2):
                lead = Lead(customer_name='Synthetic', status='assigned', source_type='manual', source_name_snapshot='test', created_by='test', updated_by='test')
                db.add(lead); db.flush()
                conv = Conversation(lead_id=lead.id, worker_id=owner.id, status='waiting_sales_reply')
                db.add(conv); db.flush()
                db.add(HandoffEvent(conversation_id=conv.conversation_id, handoff_reason_code='AI_ENGINE_RETRY_EXHAUSTED', notify_status='succeeded'))
                binding = WechatSessionBinding(conversation_id=conv.conversation_id, lead_id=lead.id, worker_id=owner.id,
                    remark_code=['CJ3N95EU','CJDZSKVN'][index], display_name='Synthetic', rpa_session_key=f'test-{index}',
                    row_fingerprint=f'row-{index}', bind_status='bound', listen_status='listening', allow_listening=True,
                    last_read_conversation_status='waiting_sales_reply', next_read_due_at=utcnow()-timedelta(minutes=5))
                db.add(binding); db.flush()
                rows.append({'lead_id':lead.id, 'conversation_id':conv.conversation_id, 'binding_id':binding.id})
            sales = Sales(sales_name='Synthetic Windows recovery', phone='13800009992', worker_id=worker['id'], enabled=True)
            db.add(sales); db.flush()
            db.get(Conversation, rows[0]['conversation_id']).sales_id = sales.id
            db.get(WechatSessionBinding, rows[0]['binding_id']).sales_id = sales.id
            db.commit()
        cfg.update(worker=worker, rows=rows)
        gate.write_json(cfg['config_path'], cfg)
    else:
        row = cfg['rows'][0]
        with SessionLocal() as db:
            for handoff in db.scalars(select(HandoffEvent).where(HandoffEvent.conversation_id == row['conversation_id'])):
                handoff.deleted_at = utcnow()
            db.get(Conversation, row['conversation_id']).status = 'sales_replied_waiting_user'
            binding = db.get(WechatSessionBinding, row['binding_id'])
            binding.last_read_conversation_status = 'sales_replied_waiting_user'
            binding.next_read_due_at = utcnow() - timedelta(seconds=1)
            db.commit()


def serve(cfg, source, label):
    sys.path[:0] = [str(Path(source) / 'backend')]
    from app.main import app
    from app.core.config import get_settings
    from app.services import c3_service
    from app.services.ai_adapter import AIEngineDecision
    import uvicorn
    class ControlledModel:
        def generate_reply_decision(self, **kwargs):
            return AIEngineDecision(decision='send_reply', reply_text='您好，请问您的预算是多少？', confidence=.95,
                guard_result='pass', evidence_refs=[], risk_flags=[], raw_payload={'adapter': 'controlled Windows fixture'})
    get_settings().c3_ai_adapter_mode = 'real'
    c3_service.get_ai_engine_adapter = ControlledModel
    @app.middleware('http')
    async def record(request, call_next):
        response = await call_next(request)
        if '/workers/' in request.url.path:
            with (Path(cfg['folder']) / (label + '-http.jsonl')).open('a', encoding='utf-8') as log:
                log.write(json.dumps({'path': request.url.path, 'method': request.method, 'status': response.status_code}) + '\n')
        return response
    uvicorn.run(app, host='127.0.0.1', port=cfg['port'], ssl_certfile=cfg['cert'], ssl_keyfile=cfg['key'],
                log_level='warning', access_log=False, lifespan='off')


@contextmanager
def backend(cfg, source, label, env):
    import requests
    with (Path(cfg['folder']) / (label + '-backend.log')).open('w', encoding='utf-8') as log:
        proc = subprocess.Popen([sys.executable, __file__, '--serve', cfg['config_path'], '--source', str(source), '--label', label], env=env, stdout=log, stderr=log)
        try:
            def ready():
                assert proc.poll() is None, 'Isolated backend exited: ' + label
                try:
                    return requests.get(cfg['base'] + '/healthz', verify=cfg['cert'], timeout=1).status_code == 200
                except requests.RequestException:
                    return False
            gate.wait_for(ready, 'isolated backend ' + label)
            yield
        finally:
            proc.terminate(); proc.wait(timeout=15)


def snapshot(data):
    with sqlite3.connect(data / 'worker_client.sqlite3') as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        binding = dict(db.execute('select * from binding where id=1').fetchone())
        control = json.loads(db.execute("select value from client_settings where key='runtime_control_v1'").fetchone()[0])
        outbox = [dict(r) for r in db.execute('select outbox_id,read_run_id,conversation_id,payload_json,status from c2_ingest_outbox order by outbox_id')]
        receipts = {r[0]: json.loads(r[1]) for r in db.execute("select key,value from c2_runtime_state where key like 'inflight_finish_receipt:%'")}
        settings = [tuple(r) for r in db.execute("select * from client_settings where key='accept_schedule'")]
        return {'binding': binding, 'control': control, 'outbox': outbox, 'receipts': receipts, 'settings': settings}


def binding_difference(before, after):
    fields = sorted(k for k in set(before['binding']) | set(after['binding'])
                    if before['binding'].get(k) != after['binding'].get(k))
    return {'changed_binding_fields': fields,
            'status_before': before['binding']['run_status'], 'status_after': after['binding']['run_status'],
            'updated_at_before': before['binding'].get('updated_at'),
            'updated_at_after': after['binding'].get('updated_at'),
            'binding_values_redacted': True}


def assert_identity(before, after):
    # save_binding changes updated_at when paused becomes faulted. This is a
    # recovery-state transition, not an Updater frozen-data baseline comparison.
    changing = {'run_status', 'updated_at'}
    assert {k: v for k, v in before['binding'].items() if k not in changing} == {
        k: v for k, v in after['binding'].items() if k not in changing}, 'Binding identity changed'
    old_status, new_status = before['binding']['run_status'], after['binding']['run_status']
    assert old_status == new_status or (old_status, new_status) == ('paused', 'faulted'), 'Unexpected binding status transition'
    old_time, new_time = before['binding'].get('updated_at'), after['binding'].get('updated_at')
    if old_time != new_time:
        from datetime import datetime
        assert (old_status, new_status) == ('paused', 'faulted'), 'Binding timestamp changed without expected transition'
        assert old_time and new_time and datetime.fromisoformat(new_time) >= datetime.fromisoformat(old_time), 'Binding timestamp moved backwards'
    assert before['settings'] == after['settings'], 'Acceptance schedule changed'
    def facts(value):
        return [{k: v for k, v in row.items() if k != 'status'} for row in value['outbox']]
    assert facts(before) == facts(after), 'Original Outbox identity or payload bytes changed'


def normal_close(proc):
    import ctypes
    from ctypes import wintypes
    user = ctypes.windll.user32
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostMessageW.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def capture(hwnd, unused):
        pid = wintypes.DWORD(); user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == proc.pid and user.IsWindowVisible(hwnd):
            windows.append(hwnd)
        return True
    user.EnumWindows(callback_type(capture), 0)
    assert windows, 'Original EXE window not found'
    for hwnd in windows:
        assert user.PostMessageW(hwnd, 0x0010, 0, 0)
    assert proc.wait(timeout=90) == 0, 'Original EXE did not close normally'


def create_pending_fixture(cfg, env):
    # Extract only the existing reviewed synthetic driver constants, without importing its tests.
    tree = ast.parse((ROOT / 'backend/tests/test_c2_historical_ocr_settlement.py').read_text(encoding='utf-8'))
    values = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
              and isinstance(n.targets[0], ast.Name) and n.targets[0].id in {'WORKER', 'OLD_TEXT', 'NEW_QUESTION'}}
    script = values['WORKER'].replace('with db_connection() as conn:', "if mode == 'old_paused':\n    assert runner.set_run_status('paused')\nwith db_connection() as conn:")
    folder = Path(cfg['folder']); path = folder / 'synthetic-old-worker.py'; path.write_text(script, encoding='utf-8')
    def fixture_op(name):
        subprocess.run([sys.executable, __file__, '--fixture-operation', name, '--config', cfg['config_path']], env=env, check=True)
    fixture_op('create'); cfg.update(gate.read_json(cfg['config_path']))
    history = {'frame_id': 'old-seed', 'messages': [{'id': 'old-self', 'type': 'text', 'sender_role': 'self', 'content': values['OLD_TEXT']}]}
    frame = {'frame_id': 'old-current', 'messages': [{**history['messages'][0], 'id': 'current-self', 'content': values['OLD_TEXT'].replace('ZX 2026', 'ZX2026')},
             {'id': 'new-customer-question', 'type': 'text', 'sender_role': 'customer', 'content': values['NEW_QUESTION']}]}
    def run(name, value):
        f = folder / (name + '-frame.json'); gate.write_json(f, value)
        old = Path(cfg['old_source'])
        oldenv = {**env, 'PYTHONPATH': os.pathsep.join(str(old / p) for p in ('worker-client', 'worker-client/tests', 'worker-client/omniauto-rpa'))}
        proc = subprocess.run([sys.executable, str(path), cfg['base'], json.dumps(cfg['worker']), cfg['rows'][0]['conversation_id'], str(f), name], env=oldenv, cwd=old, text=True, encoding="utf-8", capture_output=True, timeout=45)
        (folder / (name + '.log')).write_text(proc.stdout + proc.stderr, encoding='utf-8')
        assert proc.returncode == 0, proc.stderr[-1500:]
        return json.loads(proc.stdout.strip().splitlines()[-1])
    with backend(cfg, cfg['old_source'], 'fixture-old', env):
        first = run('seed', history); assert first['result']['ok']
        fixture_op('next-turn')
        stuck = run('old_paused', frame)
        assert stuck['saved_status'] == 'paused' and stuck['runtime']['inflight_flow_id']
        assert any(e['status'] == 409 and e['response']['code'] == 'MESSAGE_OBSERVATION_MAPPING_INCOMPLETE' for e in stuck['exchanges'] if e['url'].endswith('/messages/ingest'))
    gate.write_json(folder / 'fixture-identity.json', {'original_source': cfg['old_sha'], 'synthetic_only': True,
        'flow_id': stuck['runtime']['inflight_flow_id'], 'initial_status': 'paused', 'windows_exe_tested': False})


def run(args):
    folder = args.work_root.resolve(); folder.mkdir(parents=True, exist_ok=False)
    data = folder / 'data'; data.mkdir()
    cert, key = gate.tls_files(folder); port = gate.free_port(); debug = gate.free_port()
    oldsource = args.old_source_root.resolve()
    oldsha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=oldsource, text=True).strip()
    plan = gate.load_release_plan(getattr(args, 'plan', None))
    baseline = plan['old_client']
    assert plan['recovery'] == 'pending_read', 'Recovery case not requested'
    assert oldsha == baseline['source_commit'], 'Original source identity mismatch'
    cfg = {'folder': str(folder), 'old_source': str(oldsource), 'old_sha': oldsha, 'port': port, 'base': f'https://127.0.0.1:{port}',
           'cert': str(cert), 'key': str(key), 'config_path': str(folder / 'config.json')}
    gate.write_json(cfg['config_path'], cfg)
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('TOKEN', 'SECRET', 'API_KEY', 'SIGNING_PRIVATE'))}
    env.update(CHEJIN_WORKER_HOME=str(data), CHEJIN_RPA_MODE='mock', CHEJIN_UI_LOCK_LEASE_SECONDS='1',
        CHEJIN_API_BASE_URL=cfg['base']+'/api', CHEJIN_HEARTBEAT_INTERVAL='1', CHEJIN_API_TIMEOUT='5',
        REQUESTS_CA_BUNDLE=str(cert), SSL_CERT_FILE=str(cert), QTWEBENGINE_REMOTE_DEBUGGING=f'127.0.0.1:{debug}',
        PYTHONUTF8='1', PYTHONIOENCODING='utf-8', ENVIRONMENT='test', DATABASE_URL=os.environ['CHEJIN_PENDING_TEST_DATABASE_URL'],
        AUTO_CREATE_TABLES='true', C3_BATCH_RECOVERY_POLL_SECONDS='0', C3_AI_ADAPTER_MODE='mock', FEISHU_APP_ID='', FEISHU_APP_SECRET='')
    create_pending_fixture(cfg, env)
    if args.fixture_only:
        print('Synthetic original-source pending fixture created; no Windows acceptance claimed'); return
    assert os.name == 'nt', 'Windows EXE acceptance requires Windows'
    before = snapshot(data); flow = before['control']['inflight_flow_id']
    report = {'status': 'failed', 'mode': 'pending_read_preserve_data_install', 'current_version': baseline['version'],
              'target_version': gate.read_json(args.release)['version'], 'target_zip_sha256': gate.digest(args.archive), 'physical_wechat_send_tested': False}
    processes = []
    try:
        olddir = folder/'original'/'CheJinWorkerClient'; shutil.copytree(args.old_package_root, olddir)
        assert gate.digest(olddir/'CheJinWorkerClient.exe') == baseline['exe_sha256']
        assert gate.digest(olddir/'CheJinUpdater.exe') == baseline['updater_sha256']
        def ui_ready(proc):
            import urllib.request
            def ready():
                assert proc.poll() is None, 'Worker EXE exited'
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{debug}/json', timeout=1) as response:
                        return any(p.get('type') == 'page' for p in json.load(response))
                except OSError: return False
            gate.wait_for(ready, 'actual Worker EXE UI')
        with (folder/'process.log').open('w', encoding='utf-8') as log:
            with backend(cfg, oldsource, 'old-exe', env):
                old = subprocess.Popen([str(olddir/'CheJinWorkerClient.exe')], env=env, cwd=olddir, stdout=log, stderr=log); processes.append(old)
                ui_ready(old)
                with gate.QtPage(debug) as page:
                    page.click_button('打开设置'); page.wait_text('V'+baseline['version']); page.screenshot(folder/'before.png')
                normal_close(old)
            exited = snapshot(data)
            gate.write_json(folder/'old-exit-diff.json', binding_difference(before, exited))
            assert_identity(before, exited)
            assert exited['control']['inflight_flow_id'] == flow, 'Original pending flow unexpectedly vanished'
            sys.path.insert(0, str(ROOT/'worker-client'))
            from chejin_worker_client.models import ClientRelease
            from chejin_worker_client.release_package_contract import verify_release_signature, load_trusted_release_keys, verify_staged_package
            descriptor = gate.read_json(args.release)
            release = ClientRelease.from_api({**descriptor, 'latest_version': descriptor['version'], 'update_available': True})
            trust = olddir/'_internal/release-signing-public-keys.json'
            if not trust.exists(): trust = olddir/'release-signing-public-keys.json'
            verify_release_signature(release, trusted_keys=load_trusted_release_keys(trust))
            manifest = verify_staged_package(release, args.target_package_root)
            newdir = folder/'new-install'/'CheJinWorkerClient'; shutil.copytree(args.target_package_root, newdir)
            verify_staged_package(release, newdir)
            with backend(cfg, ROOT, 'candidate-exe', env):
                new = subprocess.Popen([str(newdir/'CheJinWorkerClient.exe')], env=env, cwd=newdir, stdout=log, stderr=log); processes.append(new)
                ui_ready(new)
                gate.wait_for(lambda: not snapshot(data)['control']['inflight_flow_id'], 'candidate EXE finishes original pending flow', 90)
                after = snapshot(data)
                report['binding_difference'] = binding_difference(exited, after)
                gate.write_json(folder/'recovery-diff.json', report['binding_difference'])
                assert_identity(exited, after)
                assert after['binding']['run_status'] == 'faulted' and after['control']['pause_requested']
                assert all(r['status'] == 'confirmed' for r in after['outbox'])
                # Successful finish removes the temporary local receipt. Its
                # durable backend completion, original Outbox and HTTP finish
                # acknowledgment are the evidence after reconciliation.
                with gate.QtPage(debug) as page:
                    page.click_button('打开设置'); page.wait_text('V'+descriptor['version']); page.screenshot(folder/'after.png')
                from sqlalchemy import create_engine, text
                engine = create_engine(env['DATABASE_URL'])
                with engine.connect() as db:
                    state = db.execute(text('select inflight_flow_state,run_status from workers where id=:wid'), {'wid':cfg['worker']['id']}).one()
                    assert not state[0] and state[1] == 'faulted', state
                    completion = db.execute(text('select last_read_run_id,last_read_completed_at from wechat_session_bindings where id=:bid'), {'bid':cfg['rows'][0]['binding_id']}).one()
                    assert completion[0] == flow and completion[1] is not None, 'Original read lacks durable completion'
                engine.dispose()
                requests = [json.loads(s) for s in (folder/'candidate-exe-http.jsonl').read_text(encoding='utf-8').splitlines()]
                assert any(r['path'].endswith('/messages/ingest') and r['status'] == 200 for r in requests)
                assert any(r['path'].endswith('/inflight-flow/finish') and r['status'] == 200 for r in requests)
                assert not any(r['path'].endswith('/inflight-flow/start') or r['path'].endswith('/claim-send') for r in requests)
                report.update(status='passed', original_exe_sha256=baseline['exe_sha256'], original_updater_sha256=baseline['updater_sha256'],
                    normal_close_used=True, original_pending_flow_preserved_at_install=True, original_data_directory_reused=True,
                    original_outbox_bytes_preserved=True, original_flow_completed=True, stopped_after_recovery=True,
                    target_ui_confirmed=True, target_commit=manifest['git_commit'], real_exe_recovery=True)
                normal_close(new)
    except Exception as exc:
        report['failure'] = str(exc)[:1000]; raise
    finally:
        gate.write_json(folder/'result.json', report)
        for proc in processes:
            if proc.poll() is None: proc.terminate(); proc.wait(timeout=15)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--plan', type=Path)
    p.add_argument('--serve', type=Path); p.add_argument('--source', type=Path); p.add_argument('--label')
    p.add_argument('--fixture-operation'); p.add_argument('--config', type=Path)
    p.add_argument('--fixture-only', action='store_true')
    for name in ('old-source-root', 'old-package-root', 'target-package-root', 'archive', 'release', 'work-root'):
        p.add_argument('--'+name, type=Path)
    a = p.parse_args()
    if a.serve: serve(gate.read_json(a.serve), a.source, a.label)
    elif a.fixture_operation: fixture_operation(a.fixture_operation, gate.read_json(a.config))
    else: run(a)


if __name__ == '__main__':
    main()
