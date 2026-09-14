"""Production profile handler -> Worker -> HTTP/PG completion, with native UI controlled.

The OCR labels are synthetic (or explicitly supplied private evidence). We do
not construct an already_friend result or manually finish the task/Flow.
"""
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select
from conftest import authenticated_admin_dependency
from test_lead_followup_eligibility import isolated_db, http_api, fixture_rows
from task_ownership_fixtures import owned_add_friend_task
import test_c1_success_receipt_http as receipt
from app.core.database import SessionLocal
from app.enums import ContactType
from app.models.task import Task, TaskEvent
from app.services import contact_utils
from app.services.lead_service import _contact_model


PROFILE_BOUNDARY = r'''
import time
sys.path.insert(0, str(Path.cwd()/'worker-client'/'omniauto-rpa'))
from apps.wechat_ai_customer_service.tests.test_add_friend_existing_contact import profile, profile_runtime
from chejin_worker_client.action_journal import list_action_journals
desktop_evidence=[]
def observed_profile(self, args, timeout=30, cancel_check=None):
    self.calls += 1
    assert args[0] == 'add-friend-entry-click-plan-windows', args
    journal = Path(args[args.index('--action-journal')+1])
    assert json.loads(journal.read_text())['transaction_id']
    if request.get('ocr_path'):
        items=json.loads(Path(request['ocr_path']).read_text())
        size=(max(x['right'] for x in items)+1,max(x['bottom'] for x in items)+1)
    else:
        items,size=profile()
    result,clicks=profile_runtime(items,size,journal.parent/'profile-replay')
    desktop_evidence.append({'result':result,'clicks':clicks})
    return result
Desktop._call_omniauto=observed_profile
def state():
    return {'binding':load_binding().run_status,'runtime':load_runtime_control(),
            'calls':bridge.calls,'desktop':desktop_evidence}
'''


@pytest.mark.parametrize('loss', ['', 'before', 'after'])
def test_profile_title_completes_and_releases_flow_through_worker(http_api, tmp_path, monkeypatch, loss):
    worker, rows = fixture_rows()
    with SessionLocal() as db:
        db.add(_contact_model(rows[0]['lead_id'], ContactType.phone,
                             contact_utils.normalize_phone('13800008880'), True))
        task = owned_add_friend_task(db, lead_id=rows[0]['lead_id'], worker_id=worker['id'],
                                     task_type='add_friend', status='pending')
        db.add(task)
        db.commit()
        task_id = task.id
    monkeypatch.setattr(receipt, 'BOUNDARY', PROFILE_BOUNDARY)
    request = {'base_url': http_api.get('/healthz').url.removesuffix('/healthz')+'/api',
               'worker_id': worker['id'], 'token': worker['worker_token'],
               'result_code': 'already_friend',
               'interruption': f'already-friend:{loss}' if loss else '',
               'ocr_path': os.environ.get('CHEJIN_EXISTING_CONTACT_OCR', '')}
    first = receipt.child(tmp_path, request)
    restart = receipt.child(tmp_path, {**request, 'interruption': ''}, restart=True)
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        terminal = {'status': task.status, 'result_code': task.result_code,
                    'events': [{'type': e.event_type, 'status': e.to_status, 'result_code': e.result_code}
                               for e in db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id))]}
    evidence = {'initial': first, 'restart': restart, 'task': terminal,
                'backend': receipt.backend_state(worker['id']),
                'boundary': 'Saved/synthetic OCR, controlled capture/window/mouse; production classifier, close, result, RpaBridge, Worker, HTTP, PostgreSQL, same SQLite restart.'}
    (tmp_path/'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    assert first['after']['calls'] == 1 and restart['after']['calls'] == 0, evidence
    desktop = first['after']['desktop']
    assert len(desktop) == 1 and desktop[0]['result']['result_code'] == 'already_friend', evidence
    assert [c['action_name'] for c in desktop[0]['clicks']] == ['already_friend_add_friend_dialog_close'], evidence
    assert terminal['status'] == 'completed' and terminal['result_code'] == 'already_friend', evidence
    assert sum(e['status'] == 'completed' for e in terminal['events']) == 1, evidence
    assert not evidence['backend']['flow'].get('flow_id'), evidence
    assert not restart['after']['runtime'].get('inflight_flow_id'), evidence
    assert len(first['injected']) == bool(loss), evidence
