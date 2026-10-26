import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timezone
from unittest.mock import Mock

SCRIPT = Path(__file__).resolve().parents[3] / 'worker-client/scripts/run-windows-client-upgrade-test.py'

def collector():
    tree = ast.parse(SCRIPT.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'collect_failure_evidence')
    env = dict(datetime=datetime,timezone=timezone,time=time,sqlite3=sqlite3,json=json,
               QtPage=Mock(side_effect=OSError('UI unavailable')),TOKEN='synthetic-token',
               WORKER_ID='synthetic-worker',read_json=lambda p:json.loads(p.read_text()))
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(SCRIPT),'exec'),env)
    return env['collect_failure_evidence']

def test_failure_reads_manifest_sized_state_and_sync_warn_without_changing_databases(tmp_path):
    data=tmp_path/'data';data.mkdir();updates=tmp_path/'updates';updates.mkdir()
    (updates/'update-state.json').write_text(json.dumps({'state':'waiting_safe_boundary','waiting_reason_code':'BACKEND_STOP_UNCONFIRMED','manifest':'x'*460000}))
    with sqlite3.connect(data/'worker_client.sqlite3') as db:
        db.execute('CREATE TABLE local_logs(created_at,event,error_code,message,metadata,level)')
        db.execute('INSERT INTO local_logs VALUES(?,?,?,?,?,?)',('now','run_status_sync_retry_failed','RUN_STATUS_SYNC_FAILED','stopped',json.dumps({'requested_run_status':'faulted','secret':'must-not-export'}),'WARN'))
    with sqlite3.connect(tmp_path/'backend.sqlite3') as db:
        db.execute('CREATE TABLE workers(id,run_status,running_status,enabled)')
        db.execute('INSERT INTO workers VALUES(?,?,?,?)',('synthetic-worker','faulted','idle',1))
    paths=[data/'worker_client.sqlite3',tmp_path/'backend.sqlite3'];before=[p.read_bytes() for p in paths]
    r=collector()(tmp_path,data,1,time.monotonic())
    assert r['control_files'][0]['values']=={'state':'waiting_safe_boundary','waiting_reason_code':'BACKEND_STOP_UNCONFIRMED'}
    assert r['local_logs']['entries'][0]['metadata']=={'requested_run_status':'faulted'}
    assert r['backend_stop_state']=={'read_status':'read','worker_found':True,'run_status':'faulted','running_status':'idle','enabled':True}
    assert [p.read_bytes() for p in paths]==before
    assert 'must-not-export' not in json.dumps(r)

def test_oversize_and_missing_data_remain_explicit(tmp_path):
    (tmp_path/'updates').mkdir();(tmp_path/'updates/update-state.json').write_bytes(b'x'*1048577)
    r=collector()(tmp_path,tmp_path/'missing',1,None)
    assert r['control_files'][0]['read_status']=='oversize_skipped'
    assert r['backend_stop_state']['read_status']=='missing'

def test_backend_symlink_outside_case_is_not_read(tmp_path):
    case=tmp_path/'case';case.mkdir();outside=tmp_path/'private.sqlite3';outside.write_text('do not read')
    (case/'backend.sqlite3').symlink_to(outside)
    assert collector()(case,case/'missing',1,None)['backend_stop_state']['read_status']=='outside_case_skipped'
