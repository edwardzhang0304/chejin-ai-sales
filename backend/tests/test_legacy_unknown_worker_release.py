"""Existing Worker recovery and unchanged publication entry after server timeout.

Optionally run the actual archived 0.9.90 Python source. Desktop operations
and external delivery are controlled; this is not packaged Windows UAT.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.contracts.c2 import contract_revision, contract_sha256
from app.models.base import utcnow
from app.models.c3 import ReplyAction, SentAck
from app.models.worker import Worker
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from test_send_settlement_readiness import terminal_timeout
from test_worker_fault_recovery import WORKER_PROCESS

ROOT=Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('source_kind',['current','released_090'])
def test_same_sqlite_remains_stopped_until_explicit_start_then_release_gate_passes(
        http_api, monkeypatch, async_generation, tmp_path, source_kind):
    source=ROOT/'worker-client'
    if source_kind=='released_090':
        configured=os.environ.get('CHEJIN_LEGACY_WORKER_SOURCE')
        if not configured:pytest.skip('Archived released 0.9.90 source must be provided explicitly')
        source=Path(configured)
        assert '__version__ = "0.9.90"' in (source/'chejin_worker_client/__init__.py').read_text()
    worker,ids,_,headers,_,_=terminal_timeout(http_api,monkeypatch)
    # Same production Worker entry as the adjacent recovery tests; change only
    # the test-driver start gesture and initial binding setup. Second launch
    # reuses the persisted SQLite without reinitializing its binding.
    script=WORKER_PROCESS.replace('save_binding(binding)',
        "\nif load_binding() is None: save_binding(binding)\nelse: assert load_binding().worker_id == binding.worker_id")
    script=script.replace('first=runner.set_run_status("running")',
        'first=runner.set_run_status("running") if request["allow_start"] else False')
    script=script.replace('second=runner.set_run_status("running")','second=False')
    # Preserve the existing possible-send identity marker too: the incident
    # had no pending Outbox or Flow, but still retained this no-resend evidence.
    with SessionLocal() as db:
        reply_text=db.get(ReplyAction,ids['reply_action_id']).reply_text
    seed = """
from types import SimpleNamespace
from chejin_worker_client.storage import load_c2_state
possible=request['possible_send']
key='possible_ai_sends:'+possible['conversation_id']
if request['seed_possible_send']:
    runner._record_possible_ai_send(
        target=SimpleNamespace(conversation_id=possible['conversation_id']),
        reply_action_id=possible['reply_action_id'], reply_text=possible['reply_text'],
        reply_text_hash=runner._reply_text_hash(runner._canonical_reply_text(possible['reply_text'])),
        reserved_worker_stable_id='synthetic-unknown-worker-message',pre_frame_id='synthetic-send-pre',
        pre_action_identity_sequence=[])
marker_before=load_c2_state(key)
assert marker_before['sends'][0]['physical_send_possible'] is True
"""
    script=script.replace('runner.start(load_binding())',seed+'\nrunner.start(load_binding())')
    script=script.replace('print(json.dumps(result))',
        "assert load_c2_state(key)==marker_before\nresult['possible_send_unchanged']=True\nprint(json.dumps(result))")
    data=tmp_path/'worker'
    env={**os.environ,'CHEJIN_WORKER_HOME':str(data),'CHEJIN_OBSERVABILITY_ENABLED':'false',
         'CHEJIN_TASK_POLL_INTERVAL':'0.1','CHEJIN_HEARTBEAT_INTERVAL':'0.1',
         'PYTHONPATH':os.pathsep.join([str(source),str(source/'omniauto-rpa'),os.environ.get('PYTHONPATH','')])}
    evidence=[]
    for allow_start in [False,True]:
        request={'mode':'normal','allow_start':allow_start,'seed_possible_send':not allow_start,
                 'possible_send':{'conversation_id':ids['conversation_id'],'reply_action_id':ids['reply_action_id'],'reply_text':reply_text},'url':http_api.get('/healthz').url.removesuffix('/healthz'),
                 'binding':{'worker_id':worker['id'],'worker_token':worker['worker_token'],'client_instance_id':'client-c3'}}
        path=tmp_path/('start.json' if allow_start else 'probe.json');path.write_text(json.dumps(request))
        result=subprocess.run([sys.executable,'-c',script,str(path)],env=env,capture_output=True,text=True,timeout=40)
        (tmp_path/(path.stem+'.stdout')).write_text(result.stdout)
        (tmp_path/(path.stem+'.stderr')).write_text(result.stderr)
        assert result.returncode==0,result.stdout[-2000:]+result.stderr[-3000:]
        value=json.loads(result.stdout.strip().splitlines()[-1]);evidence.append(value)
        assert value['before']=='faulted' and value['state_before']['ready'],value
        assert value['after']==('running' if allow_start else 'faulted'),value
        assert value['recoveries']==int(allow_start),value
        assert value['possible_send_unchanged'],value
        with sqlite3.connect(data/'worker_client.sqlite3') as db:
            assert db.execute("select count(*) from reply_send_ack_outbox where status in ('intent','waiting','capability_paused')").fetchone()[0]==0
    with SessionLocal() as db:
        assert db.get(ReplyAction,ids['reply_action_id']).status=='unknown_send_result'
        assert db.scalar(select(SentAck.id)) is None  # No fabricated old receipt.
    stopped=http_api.post(f"/api/workers/{worker['id']}/run-status",headers=headers,
        json={'client_instance_id':'client-c3','run_status':'paused'})
    assert stopped.status_code==200,stopped.text
    with SessionLocal() as db:
        owner=db.get(Worker,worker['id']);assert not owner.current_task and not owner.inflight_flow_state
        owner.last_heartbeat_at=utcnow()-timedelta(seconds=121)  # Simulated offline duration; do not wait two minutes.
        db.commit()
    request={'contract_revision':contract_revision(),'contract_sha256':contract_sha256(),'approved_read_flows':{}}
    gate=subprocess.run([sys.executable,str(ROOT/'ops/formal_release/manual_readiness.py'),json.dumps(request)],
                        env=os.environ.copy(),capture_output=True,text=True,timeout=20)
    (tmp_path/'manual-readiness.stdout').write_text(gate.stdout);(tmp_path/'manual-readiness.stderr').write_text(gate.stderr)
    assert gate.returncode==0,gate.stdout+gate.stderr
    assert json.loads(gate.stdout)['ready'] is True
    (tmp_path/'result.json').write_text(json.dumps({'worker_source':str(source),'source_kind':source_kind,
        'same_sqlite':True,'probe_then_explicit_start':evidence,'gate':json.loads(gate.stdout)},indent=2))
