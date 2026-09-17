"""Protocol recovery through real Worker/HTTP/PG/SQLite, no desktop actions.

The read-failure budget is explicit protocol setup; the separate OCR suite
proves how it is produced. This test covers lost claim/query responses and
never claims that a missing action journal proves an unsent message.
"""
from datetime import timedelta

import pytest
import requests

from test_pre_send_read_recovery import setup_receipt
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.c3 import ReplyAction, SentAck
from app.models.task import Task
from app.models.worker import Worker
from chejin_worker_client import storage, pre_send_read_recovery as recovery
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding, Task as LocalTask
from chejin_worker_client.rpa_bridge import RpaBridge
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.storage import begin_runtime_flow, load_runtime_control


@pytest.mark.parametrize('query_loss', ['none','request','response'])
def test_lost_claim_uses_original_permit_and_settles_unknown_without_ui(
        http_api,monkeypatch,async_generation,tmp_path,query_loss):
    monkeypatch.setattr(storage,'APP_DIR',tmp_path/'worker')
    monkeypatch.setattr(storage,'DB_FILE',tmp_path/'worker/worker_client.sqlite3')
    worker,ids,_,headers,body=setup_receipt(http_api,monkeypatch,claimed=True,stopped=False)
    binding=Binding(worker['id'],worker['worker_token'],'client-c3',run_status='running')
    storage.save_binding(binding)
    begin_runtime_flow(ids['flow_id'],'chat_reply')
    client=WorkerApiClient(str(http_api.get('/healthz').url).removesuffix('/healthz')+'/api')
    client.inflight_flow_id=ids['flow_id']
    with SessionLocal() as db:
        task=LocalTask.from_api(__import__('app.services.task_service',fromlist=['task_to_detail']).task_to_detail(db.get(Task,ids['task_id'])))
    client._remember_task_lease(task)
    proof=body['evidence']['pre_send_read_failure']
    recovery.reserve(ids,proof['first_failure'])
    recovery.complete_attempt(ids['reply_action_id'])
    recovery.record_claim_attempt(ids['reply_action_id'],lease_fencing_token=task.lease_fencing_token)
    native_send=client.session.send
    lost=[]
    def lose_claim_response(request,**kw):
        response=native_send(request,**kw)
        if request.url.endswith('/claim-send') and not lost:
            assert response.status_code==200
            lost.append(True)
            raise requests.ConnectionError('test: original claim committed; response lost')
        return response
    monkeypatch.setattr(client.session,'send',lose_claim_response)
    with pytest.raises(requests.ConnectionError): client.claim_send(binding,task)
    recovery.interrupt(ids['reply_action_id'],'claim_response_unavailable')
    bridge=RpaBridge()
    ui=[]
    def forbidden(*args,**kwargs):
        ui.append(args)
        raise AssertionError('Receipt recovery must not call a desktop action')
    monkeypatch.setattr(bridge,'_call_omniauto',forbidden)
    runner=TaskRunner(client,bridge,on_profile=lambda _:None,on_status=lambda _:None,
                      on_step=lambda _:None,on_task=lambda _:None,on_result=lambda _:None,on_error=lambda _:None)
    runner.binding=binding
    assert runner.set_run_status('faulted')
    with SessionLocal() as db:
        db.get(Task,ids['task_id']).lease_expires_at=utcnow()-timedelta(seconds=5)
        original_token=db.get(ReplyAction,ids['reply_action_id']).send_token
        db.commit()
    query_attempts=[]
    def query_transport(request,**kw):
        is_query=request.url.endswith('/claim-send')
        if is_query:
            query_attempts.append(True)
            if query_loss=='request' and len(query_attempts)==1:
                raise requests.ConnectionError('test: query request not delivered')
        response=native_send(request,**kw)
        if is_query and query_loss=='response' and len(query_attempts)==1:
            raise requests.ConnectionError('test: query response lost')
        return response
    monkeypatch.setattr(client.session,'send',query_transport)
    if query_loss!='none':
        assert runner._replay_reply_send_ack_outbox(binding) is False
        with SessionLocal() as db: assert db.query(SentAck).count()==0
        assert binding.run_status=='faulted' and not ui
    assert runner._replay_reply_send_ack_outbox(binding)
    with SessionLocal() as db:
        ack=db.query(SentAck).one()
        assert ack.send_token==original_token
        assert ack.send_result=='unknown' and ack.action_phase=='trigger_attempted'
        assert db.get(ReplyAction,ids['reply_action_id']).status=='unknown_send_result'
        assert db.get(Worker,worker['id']).run_status=='faulted'
    runner._finish_inflight_flow(binding,flow_id=ids['flow_id'],terminal_kind='task_terminal',
                                 conversation_id=ids['conversation_id'])
    assert not load_runtime_control().get('inflight_flow_id')
    assert not recovery.settlement_pending() and not ui
    assert storage.load_reply_send_ack_outbox(ids['reply_action_id'])['status']=='confirmed'
