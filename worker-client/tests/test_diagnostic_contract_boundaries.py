"""Real intake/log/projection -> desktop collector ZIP; synthetic data only."""
import hashlib
import json
import zipfile
import pytest
from test_failure_evidence_boundaries import home, package_for
from test_hook_facts import SCOPE, incoming
from chejin_worker_client import storage, diagnostic_bundle as bundle
from chejin_worker_client.hook_inbox import NativeInbox
from chejin_worker_client.hook_projection import create_projection_store
from chejin_worker_client.diagnostic_callback import summarize_callback


def body(messages):
    return json.dumps({'selfwxid': SCOPE.self_wxid, 'ServerPort': str(SCOPE.hook_api_port),
        'sendorrecv': '2', 'msgnumber': str(len(messages)), 'msglist': messages}).encode()


def produce(count, *, invalid=False, large=False):
    messages = [incoming(msgsvrid=str(8000000000000000000+i), msg='private-synthetic-body') for i in range(count)]
    if invalid:
        messages[0] = None
    if large:
        messages[0]['msg'] = 'private-synthetic-body'*4000
    raw = body(messages)
    inbox = NativeInbox(max_body_bytes=256*1024, max_inbox_bytes=16*1024*1024)
    saved = inbox.persist(raw, SCOPE)
    return saved['receive_seq'], raw


def evidence(path):
    with zipfile.ZipFile(path) as z:
        return {name: json.loads(z.read(name)) for name in z.namelist() if name.endswith('.json')}


def assert_source(seq, raw):
    with storage.db_connection() as db:
        saved = db.execute('SELECT raw_body FROM hook_raw_inbox WHERE receive_seq=?', (seq,)).fetchone()[0]
    assert hashlib.sha256(saved).digest() == hashlib.sha256(raw).digest()


@pytest.mark.parametrize('level,code,force,expected', [
    ('WARN', None, False, True), ('WARNING', None, False, True), ('ERROR', None, False, True),
    ('CRITICAL', None, False, True), ('INFO', 'HOOK_PROJECTION_INVALID', False, True),
    ('INFO', None, True, True), ('INFO', None, False, False)])
def test_log_producer_and_export_share_fault_priority(home, level, code, force, expected):
    inbox = NativeInbox(max_body_bytes=65536, max_inbox_bytes=16*1024*1024)
    for i in range(1001):
        source = body([incoming(msgsvrid=str(8000000000000000000+i), msg='private-synthetic-body')])
        seq = inbox.persist(source, SCOPE)['receive_seq']
    result = storage.append_log(level, 'bounded_fault_test', 'synthetic failure', error_code=code,
        force_incident=force, metadata={'receive_seq': seq}, include_exception_text=False)
    if expected:
        assert result['incident_id']
        package_for('bounded_fault_test')  # Actual incident completion, not a fake package.
    saved = evidence(bundle.export_bundle(home, home/'result.zip'))
    rows = saved['state/hook_raw_inbox.json']['rows']
    assert len(rows) == 1000 and (seq in {r['receive_seq'] for r in rows}) == expected
    assert saved['export.json']['partial']
    assert 'private-synthetic-body' not in json.dumps(saved)
    assert_source(seq, source)


@pytest.mark.parametrize('count', [100, 101, 150, 200, 201])
def test_callback_contract_projection_and_export(home, count):
    seq, raw = produce(count)
    projection = create_projection_store().project_next(SCOPE)
    if count <= 200:
        assert projection['status'] == 'projected' and len(projection['observations']) == count
    else:
        assert projection['status'] != 'projected'
    saved = evidence(bundle.export_bundle(home, home/'result.zip'))
    row = saved['state/hook_raw_inbox.json']['rows'][0]
    assert len(row['callback_messages']) == min(count, 200)
    assert [m['msgsvrid'] for m in row['callback_messages']] == [str(8000000000000000000+i) for i in range(min(count, 200))]
    summary = row['callback_summary']
    assert summary['original_count'] == count and summary['omitted_count'] == max(0, count-200)
    assert summary['reasons'] == (['callback_entry_limit'] if count > 200 else [])
    if count > 200:
        assert any(r.get('receive_seq') == seq and r.get('reason') == 'callback_entry_limit'
                   for r in saved['export.json']['sources'])
    assert 'private-synthetic-body' not in json.dumps(saved)
    assert_source(seq, raw)


@pytest.mark.parametrize('variant,reason', [('bad_member', 'callback_invalid_member'),
    ('large', 'callback_body_size_limit'), ('table', 'table_size_limit'),
    ('total', 'callback_snapshot_total_size_limit')])
def test_omissions_identify_the_original_packet(home, monkeypatch, variant, reason):
    seq, raw = produce(150, invalid=variant=='bad_member', large=variant=='large')
    create_projection_store().project_next(SCOPE)
    kwargs = {}
    if variant == 'table':
        monkeypatch.setattr(bundle, 'MAX_FILE_BYTES', 1024)
    if variant == 'total':
        kwargs['max_bytes'] = 1024
    saved = evidence(bundle.export_bundle(home, home/'result.zip', **kwargs))
    assert any(r.get('receive_seq') == seq and r.get('reason') == reason for r in saved['export.json']['sources'])
    if variant in {'table', 'total'}:
        issue = next(r for r in saved['export.json']['sources'] if r.get('receive_seq') == seq and r.get('reason') == reason)
        assert issue['original_count'] == 150 and issue['collected_count'] == 0 and issue['omitted_count'] == 150
    if variant in {'bad_member', 'large'}:
        summary = saved['state/hook_raw_inbox.json']['rows'][0]['callback_summary']
        assert summary['original_count'] == (150 if variant == 'bad_member' else None)
        assert summary['omitted_count'] == (1 if variant == 'bad_member' else None)
    assert 'private-synthetic-body' not in json.dumps(saved)
    assert_source(seq, raw)


def test_callback_budget_expires_mid_packet_without_silent_loss(home, monkeypatch):
    seq, raw = produce(150)
    original = bundle.summarize_callback
    def short_budget(body, *, raw_bytes, expired):
        checks = iter([False, False, True])
        return original(body, raw_bytes=raw_bytes, expired=lambda: next(checks, True))
    # Time-budget boundary only; real summary and exporter still run.
    monkeypatch.setattr(bundle, 'summarize_callback', short_budget)
    saved = evidence(bundle.export_bundle(home, home/'result.zip'))
    summary = saved['state/hook_raw_inbox.json']['rows'][0]['callback_summary']
    assert summary == {'original_count': 150, 'collected_count': 2, 'omitted_count': 148,
                       'reasons': ['callback_time_limit']}
    assert any(r.get('receive_seq') == seq and r.get('omitted_count') == 148
               for r in saved['export.json']['sources'])
    assert_source(seq, raw)


@pytest.mark.parametrize('body', [b'not-json', b'[]', b'{"msglist":null}'])
def test_invalid_callback_is_explicit(body):
    entries, summary = summarize_callback(body, raw_bytes=len(body), expired=lambda: False)
    assert entries == [] and summary['original_count'] is None
    assert summary['reasons'] == ['callback_invalid_json_or_shape']


@pytest.mark.parametrize('legacy', ['forced_incident', 'missing_error_column', 'bad_metadata'])
def test_legacy_logs_remain_readable_without_schema_changes(home, legacy):
    import sqlite3
    inbox = NativeInbox(max_body_bytes=65536, max_inbox_bytes=16*1024*1024)
    for i in range(1001):
        raw = body([incoming(msgsvrid=str(8000000000000000000+i), msg='private-synthetic-body')])
        seq = inbox.persist(raw, SCOPE)['receive_seq']
    storage.append_log('INFO', 'legacy_fixture', 'synthetic', metadata={'receive_seq': seq})
    # Migrate only the synthetic fixture backwards to the persisted legacy shape.
    # The real exporter must neither migrate nor initialize it.
    with sqlite3.connect(home/'worker_client.sqlite3') as db:
        if legacy == 'forced_incident':
            db.execute('UPDATE local_logs SET metadata=?', (json.dumps({'receive_seq':seq, 'incident_id':'INC-synthetic-legacy'}),))
        else:
            db.execute("UPDATE local_logs SET level='WARN'")
        if legacy == 'missing_error_column':
            db.execute('ALTER TABLE local_logs DROP COLUMN error_code')
        if legacy == 'bad_metadata':
            # A later malformed row must not block the earlier valid reference.
            db.execute("INSERT INTO local_logs(level,event,message,metadata,created_at) VALUES('WARN','bad','synthetic','{broken', '2026-01-01')")
    before = hashlib.sha256((home/'worker_client.sqlite3').read_bytes()).hexdigest()
    saved = evidence(bundle.export_bundle(home, home/'legacy.zip'))
    assert seq in {r['receive_seq'] for r in saved['state/hook_raw_inbox.json']['rows']}
    assert hashlib.sha256((home/'worker_client.sqlite3').read_bytes()).hexdigest() == before
    assert_source(seq, raw)


def test_desktop_button_exports_full_callback_without_blocking(home):
    import os, subprocess, sys
    seq, raw = produce(200)
    assert create_projection_store().project_next(SCOPE)['status'] == 'projected'
    code = r'''
import sys,time,json,zipfile
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from chejin_worker_client.diagnostic_ui import DiagnosticDialog
from chejin_worker_client.diagnostic_bundle import Collector
app=QApplication([])
dialog=DiagnosticDialog(Path(sys.argv[1]))
dialog.open_folder=lambda:None
original=Collector.database
def slow(self,*args,**kwargs):
    time.sleep(.1)
    return original(self,*args,**kwargs)
Collector.database=slow
ticks=[]
timer=QTimer();timer.timeout.connect(lambda:ticks.append(1));timer.start(5)
dialog.show();dialog.button.click()
assert not dialog.button.isEnabled() and not dialog.job.start()
deadline=time.monotonic()+15
while dialog.result_path is None and time.monotonic()<deadline:
    app.processEvents();time.sleep(.001)
assert dialog.result_path is not None and len(ticks)>10
with zipfile.ZipFile(dialog.result_path) as archive:
    row=json.loads(archive.read('state/hook_raw_inbox.json'))['rows'][0]
    assert row['callback_summary']['collected_count']==200
    assert row['callback_summary']['omitted_count']==0
    assert 'private-synthetic-body' not in archive.read('state/hook_raw_inbox.json').decode()
print(json.dumps({'ui_timer_ticks':len(ticks),'zip':str(dialog.result_path),'receive_seq':row['receive_seq']}))
'''
    subprocess.run([sys.executable, '-c', code, str(home)], check=True, timeout=25,
                   env={**os.environ, 'QT_QPA_PLATFORM':'offscreen'})
    assert_source(seq, raw)


def test_warn_fault_links_intake_projection_delivery_and_desktop_zip(home):
    import time
    from PySide6.QtCore import QCoreApplication, QTimer
    from chejin_worker_client.diagnostic_ui import ExportJob
    from test_diagnostic_priority_chain import produce as produce_chain, rows_before
    events = produce_chain(1001, pending=True)
    event, received, target = events[-1]
    seq = received['receive_seq']
    storage.append_log('WARN', 'current_chain_fault', 'synthetic', metadata={'receive_seq':seq})
    package_for('current_chain_fault')
    before = rows_before()
    app = QCoreApplication.instance() or QCoreApplication([])
    job = ExportJob(home)
    completed, failures, ticks = [], [], []
    job.completed.connect(completed.append);job.failed.connect(failures.append)
    timer = QTimer();timer.timeout.connect(lambda:ticks.append(1));timer.start(2)
    assert job.start() and not job.start()
    deadline = time.monotonic()+15
    while not completed and not failures and time.monotonic()<deadline:
        app.processEvents();time.sleep(.001)
    timer.stop()
    assert completed and not failures and ticks
    saved = evidence(completed[0])
    for table in ('hook_raw_inbox', 'hook_observation_projection', 'hook_delivery_outbox'):
        rows = saved['state/'+table+'.json']['rows']
        assert len(rows)==1000 and rows[0]['receive_seq']==seq
    raw_row = saved['state/hook_raw_inbox.json']['rows'][0]
    outbox_row = saved['state/hook_delivery_outbox.json']['rows'][0]
    assert raw_row['event_uuid'] == received['event_uuid']
    assert outbox_row['binding_id'] == target.binding_id
    assert rows_before() == before
    assert 'private synthetic body' not in json.dumps(saved)
    print(json.dumps({'zip':str(completed[0]), 'receive_seq':seq,
                      'event_uuid':received['event_uuid'], 'binding_id':target.binding_id, 'ui_ticks':len(ticks)}))
