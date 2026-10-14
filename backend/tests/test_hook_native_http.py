"""Native admission fixtures are controlled DB evidence, not Windows UI proof."""
from copy import deepcopy
from dataclasses import replace
import json
import uuid

import pytest
from sqlalchemy import select, func
from app.core.database import SessionLocal
from app.models.hook_native import HookNativeReceipt
from app.models.task import Task, TaskEvidence
from app.models.wechat import WechatSessionBinding, MessageEvent
from app.models.c3 import MessageBatch
from app.models.worker import Worker
from app.contracts.shared_rules import shared_adapter
from test_hook_account_http import client, worker, context, heartbeat
from test_hook_account_client import identity_client
from chejin_worker_client.api import ApiError
from chejin_worker_client.hook_backend import HookWorkerCredentials
from chejin_worker_client.hook_outbox import HookDeliveryClaim


def prepared():
    w=worker()
    account=heartbeat(w,context()).json()['data']['hook_context']
    lead=client.post('/api/leads',json={'customer_name':'Native test A','phones':['13800000003']}).json()['data']
    with SessionLocal() as db:
        origin=db.scalar(select(Task).where(Task.lead_id==lead['id'],Task.task_type=='add_friend'))
        assert origin and origin.worker_id==w['id']
        origin.status='completed';origin.result_code='invite_sent'
        evidence=TaskEvidence(task_id=origin.id,worker_id=w['id'],evidence_type='hook_invite_submission',
            extra_metadata={'schema_version':'hook-invite-proof-v1','shortcode':'CJH5N28',
                'self_wxid':account['self_wxid'],'account_epoch':account['account_epoch'],
                'shortcode_written':True,'invitation_submitted':True})
        db.add(evidence);db.flush()
        binding=WechatSessionBinding(lead_id=lead['id'],sales_id=account['sales_id'],worker_id=w['id'],
            display_name='Native test A',remark_code='CJH5N28',rpa_session_key=None,row_fingerprint=None,
            bind_status='bound',allow_listening=True,listen_status='listening',
            native_self_wxid=account['self_wxid'],native_peer_wxid='wxid_customer_A',
            native_context={'account_epoch':account['account_epoch'],'source_task_id':origin.id,'invite_evidence_id':evidence.id})
        db.add(binding);db.flush();bid=binding.id;db.commit()
    rules=shared_adapter('hook_contract_rules')
    payload={'transport':'hook','schema_version':'hook-v1','worker_id':w['id'],'client_instance_id':w['instance'],
        'self_wxid':account['self_wxid'],'account_epoch':account['account_epoch'],'process_generation':'process-1',
        'binding_id':bid,'binding_revision':1,'peer_wxid':'wxid_customer_A','source_event_uuid':str(uuid.uuid4()),
        'receive_seq':'1','raw_evidence_digest':'a'*64,'observed_at':'2026-10-09T12:00:00+00:00',
        'messages':[{'native_msg_id':'6594133108973694888','entry_position':0,'direction':'incoming','message_type':'text',
            'content':'预算12.5万元\n不要现在报价🙂','source':'native_callback','source_revision':'b'*64,
            'source_time_raw':'2026-10-09 20:00:00','media_stage':None,'ordering_fields':{},'database':None,
            'database_generation':None,'from_wxid':'wxid_customer_A','to_wxid':account['self_wxid'],
            'conversation_type':'private','raw_direction':'2'}]}
    payload['event_uuid']=rules.delivery_id(payload['source_event_uuid'],w['id'],w['instance'],account['account_epoch'],bid,1)
    return w,payload


def post(w,payload,**kwargs):
    return client.post(f"/api/workers/{w['id']}/wechat/messages/native-observations",json=payload,
        headers={'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance']},**kwargs)


def test_real_worker_http_repeat_receipt_is_durable_without_domain_or_ai_side_effects(identity_client):
    w,payload=prepared();rules=shared_adapter('hook_contract_rules')
    claim=HookDeliveryClaim(payload['event_uuid'],str(uuid.uuid4()),rules.digest(payload),rules.canonical(payload),1)
    credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
    first=identity_client.store_observations(credentials,claim)
    assert first==identity_client.store_observations(credentials,claim)==rules.receipt(payload)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(HookNativeReceipt))==1
        record=db.scalar(select(HookNativeReceipt))
        assert record.dispatch_status=='pending' and record.payload_json==payload
        assert db.scalar(select(func.count()).select_from(MessageEvent))==0
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
        assert db.get(Worker,w['id']).run_status=='paused'
    with pytest.raises(ApiError) as error:
        identity_client.store_observations(replace(credentials,worker_token='invalid-token'),claim)
    assert error.value.status_code==401


def test_same_event_changed_body_is_rejected_and_original_receipt_is_preserved(identity_client):
    w,payload=prepared()
    assert post(w,payload).status_code==200
    altered=deepcopy(payload);altered['messages'][0]['content']='different body'
    bad=post(w,altered)
    assert bad.status_code==409 and bad.json()['code']=='HOOK_DELIVERY_ID_CONFLICT'
    with SessionLocal() as db:
        assert db.scalar(select(HookNativeReceipt)).payload_json==payload


@pytest.mark.parametrize('change', ['missing_proof','already_friend','other_peer','stale_revision','account_changed'])
def test_native_admission_rejects_unverified_or_stale_identity_without_storing(identity_client,change):
    w,payload=prepared()
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,payload['binding_id'])
        if change=='missing_proof': binding.native_context={**binding.native_context,'invite_evidence_id':None}
        elif change=='already_friend': db.get(Task,binding.native_context['source_task_id']).result_code='already_friend'
        elif change=='other_peer': binding.native_peer_wxid='wxid_customer_B'
        elif change=='stale_revision': binding.authorization_revision=2
        db.commit()
    if change=='account_changed': heartbeat(w,context('wxid_sales_B'))
    response=post(w,payload)
    assert response.status_code==409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(HookNativeReceipt))==0


def test_existing_receipt_can_be_acknowledged_after_revocation_but_new_event_cannot(identity_client):
    w,payload=prepared();rules=shared_adapter('hook_contract_rules')
    first=post(w,payload).json()['data']
    with SessionLocal() as db:
        db.get(WechatSessionBinding,payload['binding_id']).allow_listening=False;db.commit()
    assert post(w,payload).json()['data']==first
    changed=deepcopy(payload);changed['source_event_uuid']=str(uuid.uuid4())
    changed['event_uuid']=rules.delivery_id(changed['source_event_uuid'],w['id'],w['instance'],payload['account_epoch'],payload['binding_id'],1)
    assert post(w,changed).status_code==409


def test_strict_body_limits_duplicate_fields_and_media_are_not_default_text(identity_client):
    w,payload=prepared();rules=shared_adapter('hook_contract_rules')
    path=f"/api/workers/{w['id']}/wechat/messages/native-observations"
    headers={'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance'],'Content-Type':'application/json'}
    encoded=rules.canonical(payload)
    duplicated='{"transport":"ocr",'+encoded[1:]
    response=client.post(path,content=duplicated.encode(),headers=headers)
    assert response.status_code==400 and response.json()['code']=='HOOK_CONTRACT_DUPLICATE_FIELD'
    oversized=client.post(path,content=b'{}',headers={**headers,'Content-Length':str(rules.MAX_BYTES+1)})
    assert oversized.status_code==413 and oversized.json()['code']=='HOOK_NATIVE_BODY_TOO_LARGE'
    assert oversized.headers.get('X-Request-Id') and oversized.json()['trace_id']
    altered=deepcopy(payload);altered['messages'][0].update(message_type='voice',media_stage='metadata_observed')
    assert post(w,altered).status_code==400  # Voice XML/body is never plaintext AI input.


def test_http_callback_through_sqlite_projection_outbox_and_backend_with_lost_local_ack(identity_client,tmp_path,monkeypatch):
    import threading
    import requests
    from chejin_worker_client import storage
    from chejin_worker_client.hook_inbox import NativeInbox, HookReceiveScope
    from chejin_worker_client.hook_projection import NativeProjectionStore
    from chejin_worker_client.hook_receiver import create_receiver
    from chejin_worker_client.hook_outbox import HookDeliveryOutbox, HookDeliveryScope
    w,payload=prepared()
    monkeypatch.setattr(storage,'APP_DIR',tmp_path)
    monkeypatch.setattr(storage,'DB_FILE',tmp_path/'worker_client.sqlite3')
    receive=HookReceiveScope(payload['self_wxid'],payload['account_epoch'],'process-1',30001)
    delivery=HookDeliveryScope(w['id'],w['instance'],receive.self_wxid,receive.account_epoch,payload['binding_id'],1,payload['peer_wxid'])
    server=create_receiver(port=0,inbox=NativeInbox(max_body_bytes=65536,max_inbox_bytes=1048576),scope=receive,
        request_timeout_seconds=2,on_persistence_failure=lambda:pytest.fail('Unexpected receiver persistence failure'))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        raw=json.dumps({'ServerPort':'30001','selfwxid':receive.self_wxid,'sendorrecv':'2','msgnumber':'1',
            'msglist':[{'msgtype':'1','msgsvrid':'6594133108973694888','fromtype':'1','fromid':delivery.peer_wxid,
                'toid':receive.self_wxid,'time':'2026-10-09 20:00:00','msg':'预算12.5万元\n不要现在报价🙂'}]},ensure_ascii=False).encode()
        response=requests.post(f'http://127.0.0.1:{server.server_address[1]}/receiveChatBotMsg',data=raw,
            headers={'Content-Type':'application/json'},timeout=2)
        assert response.status_code==200
        projection=NativeProjectionStore(max_projection_bytes=1048576,max_entries=200).project_next(receive)
        outbox=HookDeliveryOutbox(max_storage_bytes=1048576)
        outbox.enqueue_projection(projection['receive_seq'],delivery,now='2026-10-09T12:00:00+00:00')
        first=outbox.claim_next(delivery,now='2026-10-09T12:00:00+00:00',lease_seconds=10)
        credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
        # Deliberately lose the local acknowledgement after backend commit.
        receipt=identity_client.store_observations(credentials,first)
        resumed=HookDeliveryOutbox(max_storage_bytes=1048576).claim_next(delivery,now='2026-10-09T12:00:11+00:00',lease_seconds=10)
        assert resumed.payload_json==first.payload_json and resumed.attempt_count==2
        repeated=identity_client.store_observations(credentials,resumed)
        assert repeated==receipt
        assert outbox.mark_stored(resumed,repeated,now='2026-10-09T12:00:12+00:00')
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(HookNativeReceipt))==1
        with storage.db_connection() as db:
            assert db.execute('SELECT status FROM hook_delivery_outbox').fetchone()[0]=='stored'
            assert db.execute('SELECT count(*) FROM hook_raw_inbox').fetchone()[0]==1
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
        assert not thread.is_alive()
