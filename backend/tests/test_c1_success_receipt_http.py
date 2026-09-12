"""Architect counterexamples retained as regression: real HTTP/PG/Worker/SQLite; controlled desktop/loss."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select
from conftest import authenticated_admin_dependency
from test_lead_followup_eligibility import isolated_db, http_api, fixture_rows
from test_worker_failure_consistency import COMMON
from app.core.database import SessionLocal
from app.models.worker import Worker
from app.models.task import Task
from app.enums import ContactType
from app.services.lead_service import _contact_model
from app.services import contact_utils

ROOT = Path(__file__).resolve().parents[2]

BOUNDARY = r'''
import time
from chejin_worker_client.action_journal import update_action_journal_item, list_action_journals
def successful_desktop(self, args, timeout=30, cancel_check=None):
    self.calls += 1
    assert args[0] == 'add-friend-entry-click-plan-windows', args
    path = Path(args[args.index('--action-journal')+1])
    payload = json.loads(path.read_text())
    task_id = payload['transaction_id']
    if request['result_code'] == 'already_friend':
        # Production already-friend detection returns success without an invite
        # trigger/confirmed journal; do not manufacture one in this fixture.
        return {'ok':True, 'task_status':'completed', 'result_code':'already_friend',
                'current_step':'searching_contact', 'message':'Controlled existing friend'}
    update_action_journal_item(path, journal_item_id=task_id,
        action_phase='trigger_attempted', business_state='invite_confirm_click_starting')
    terminal = {'ok':True, 'task_status':'completed', 'result_code':'invite_sent',
                'current_step':'invite_confirm_clicked'}
    update_action_journal_item(path, journal_item_id=task_id,
        action_phase='confirmed', business_state='invite_sent',
        business_result_confirmed=True, terminal_payload=terminal)
    return {**terminal, 'message':'Controlled desktop success, no physical click'}
Desktop._call_omniauto = successful_desktop
def state():
    return {'binding':load_binding().run_status, 'runtime':load_runtime_control(),
            'journals':[{'path':str(path),'payload':p} for path,p in list_action_journals(action_kinds=('add_friend','send','voice','image'))],
            'calls':bridge.calls}
'''

def child(tmp_path, request, restart=False):
    common = COMMON
    # Use the actual HTTP client's connection exception type, as emitted by
    # requests in production, rather than a builtin ConnectionError.
    common = common.replace("raise ConnectionError(", "raise __import__('requests').ConnectionError(")
    common = common.replace("'status':response.status_code}", "'status':response.status_code,'code':response.json().get('code')}")
    if restart:
        old = "binding=Binding(request['worker_id'],request['token'],'followup-test',run_status='running')\nsave_binding(binding)"
        assert common.count(old) == 1
        common = common.replace(old, 'binding=load_binding()\nassert binding is not None')
    if restart:
        program = r'''
before=state()
runner.start(binding)
time.sleep(9)
runner.stop_for_update(timeout_seconds=5)
print(json.dumps({'before':before, 'after':state(), 'http':events, 'injected':injected},default=str))
'''
    else:
        program = r'''
runner.tick_once()
first=state()
for _ in range(5):
    runner.tick_once()
    time.sleep(.1)
print(json.dumps({'first':first, 'after':state(), 'http':events, 'injected':injected},default=str))
'''
    label = 'restart' if restart else 'initial'
    script = tmp_path/f'{label}.py'
    crash_hook = r'''
import chejin_worker_client.task_runner as tr
original_save=tr.save_c2_state
def crash_after_receipt(key,value,**kwargs):
    original_save(key,value,**kwargs)
    if value.get('task_success') and not value.get('task_success_confirmed'):
        if request.get('stop_status'):runner.set_run_status(request['stop_status'])
        print(json.dumps({'first':state(),'after':state(),'http':events,'injected':['persisted-crash']},default=str),flush=True)
        import os
        os._exit(0)
tr.save_c2_state=crash_after_receipt
''' if request.get('crash_after_receipt') and not restart else ''
    script.write_text(common+BOUNDARY+crash_hook+program)
    inputs = tmp_path/f'{label}-input.json'
    inputs.write_text(json.dumps(request))
    env = {**os.environ, 'PYTHONPATH':str(ROOT/'worker-client'),
           'CHEJIN_WORKER_HOME':str(tmp_path/'worker-state'), 'CHEJIN_C2_ENABLED':'false',
           'CHEJIN_OBSERVABILITY_ENABLED':'false', 'PYTHONDONTWRITEBYTECODE':'1'}
    p = subprocess.run([sys.executable,str(script),str(inputs)],env=env,cwd=ROOT,
                       capture_output=True,text=True,timeout=35)
    (tmp_path/f'{label}.stdout').write_text(p.stdout)
    (tmp_path/f'{label}.stderr').write_text(p.stderr)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.splitlines()[-1])

def backend_state(worker_id):
    with SessionLocal() as db:
        w=db.get(Worker,worker_id)
        return {'status':w.run_status, 'flow':w.inflight_flow_state,
                'tasks':[{'id':t.id,'status':t.status,'error_code':t.error_code}
                         for t in db.scalars(select(Task).where(Task.worker_id==worker_id))]}

@pytest.mark.parametrize('result_code', ['invite_sent', 'already_friend'])
@pytest.mark.parametrize('loss', ['', 'before', 'after'])
def test_confirmed_success_closes_without_repeating_desktop(http_api,tmp_path,result_code,loss):
    interruption = f'{result_code.replace("_", "-")}:{loss}' if loss else ''
    worker,rows=fixture_rows()
    with SessionLocal() as db:
        db.add(_contact_model(rows[0]['lead_id'],ContactType.phone,
                             contact_utils.normalize_phone('13800008880'),True))
        db.add(Task(lead_id=rows[0]['lead_id'],worker_id=worker['id'],task_type='add_friend',status='pending'))
        db.commit()
    base=http_api.get('/healthz').url.rsplit('/healthz',1)[0]+'/api'
    request={'base_url':base, 'worker_id':worker['id'], 'token':worker['worker_token'],
             'interruption':interruption,'result_code':result_code}
    first=child(tmp_path,request)
    first['backend']=backend_state(worker['id'])
    # New OS process, same SQLite, action journals and backend rows; network restored.
    restart=child(tmp_path,{**request,'interruption':''},restart=True)
    restart['backend']=backend_state(worker['id'])
    evidence={'scenario':interruption or f'{result_code}:normal_control','initial':first,'restart':restart,
              'boundary':'Synthetic desktop only; real RpaBridge/TaskRunner/HTTP/PG/SQLite. No helper settles tasks or flows.'}
    (tmp_path/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    assert first['after']['calls']==1 and restart['after']['calls']==0, evidence
    if interruption:
        assert len(first['injected'])==1, evidence
    assert not restart['backend']['flow'].get('flow_id'), evidence
    assert not restart['after']['runtime'].get('inflight_flow_id'), evidence
    assert [t['status'] for t in restart['backend']['tasks']]==['completed'], evidence


@pytest.mark.parametrize('result_code',['invite_sent','already_friend'])
@pytest.mark.parametrize('stop_status',['','paused'])
def test_saved_success_crash_expiry_and_next_task(http_api,tmp_path,result_code,stop_status):
    from datetime import timedelta
    from app.models.base import utcnow
    worker,rows=fixture_rows()
    with SessionLocal() as db:
        db.add(_contact_model(rows[0]['lead_id'],ContactType.phone,contact_utils.normalize_phone('13800007771'),True))
        db.add(Task(lead_id=rows[0]['lead_id'],worker_id=worker['id'],task_type='add_friend',status='pending'));db.commit()
    request={'base_url':http_api.get('/healthz').url.removesuffix('/healthz')+'/api','worker_id':worker['id'],'token':worker['worker_token'],
             'interruption':'','result_code':result_code,'crash_after_receipt':True,'stop_status':stop_status}
    first=child(tmp_path,request)
    assert first['injected']==['persisted-crash'] and first['after']['calls']==1
    flow=first['after']['runtime']['inflight_flow_id']
    with SessionLocal() as db:
        assert db.get(Task,flow).status=='running'
        db.get(Task,flow).lease_expires_at=utcnow()-timedelta(seconds=1)
        db.add(_contact_model(rows[1]['lead_id'],ContactType.phone,contact_utils.normalize_phone('13800007772'),True))
        db.add(Task(lead_id=rows[1]['lead_id'],worker_id=worker['id'],task_type='add_friend',status='pending'));db.commit()
    restart=child(tmp_path,request,restart=True)
    restart['backend']=backend_state(worker['id'])
    evidence={'initial':first,'restart':restart,'expired_original_lease':True,'same_sqlite':True}
    (tmp_path/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    assert restart['after']['binding']==(stop_status or 'running'),evidence
    assert not restart['after']['runtime']['inflight_flow_id'],evidence
    assert not restart['backend']['flow'].get('flow_id'),evidence
    assert restart['after']['calls']==(0 if stop_status else 1),evidence
    assert sorted(t['status'] for t in restart['backend']['tasks'])==(['completed','pending'] if stop_status else ['completed','completed']),evidence
