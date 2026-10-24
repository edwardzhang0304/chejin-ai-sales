"""Real queued observation -> revocation -> transfer -> archival HTTP closure."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid
import json

import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import Conversation, ReplyAction, MessageBatch
from app.models.hook_native import HookNativeReceipt, HookVoiceReceipt
from app.models.wechat import WechatSessionBinding
from app.contracts.shared_rules import shared_adapter
from test_hook_binding_transfer import clean, successor, client, post, body, bound, upload
from test_hook_account_client import identity_client
from test_hook_account_http import heartbeat, context
from test_hook_native_media import media_coverage, voice_payload
from chejin_worker_client import storage
from chejin_worker_client.hook_inbox import HookReceiveScope, NativeInbox
from chejin_worker_client.hook_projection import NativeProjectionStore
from chejin_worker_client.hook_outbox import HookDeliveryOutbox, HookDeliveryScope
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_backend import HookWorkerCredentials


def queued(w,p,b):
    receive=HookReceiveScope(p['self_wxid'],p['account_epoch'],p['process_generation'],30001)
    raw=json.dumps({'ServerPort':'30001','selfwxid':p['self_wxid'],'sendorrecv':'2','msgnumber':'1',
        'msglist':[{'msgsvrid':'6594133108973694888','msgtype':'1','fromtype':'1','fromid':b['peer_wxid'],
            'toid':p['self_wxid'],'msg':'Synthetic pending customer message','time':'2026-10-24 20:00:00'}]}).encode()
    NativeInbox(max_body_bytes=65536,max_inbox_bytes=1048576).persist(raw,receive)
    projection=NativeProjectionStore(max_projection_bytes=1048576,max_entries=200).project_next(receive)
    delivery=HookDeliveryScope(w['id'],w['instance'],p['self_wxid'],p['account_epoch'],
        b['binding_id'],b['binding_revision'],b['peer_wxid'])
    event=HookDeliveryOutbox(max_storage_bytes=1048576).enqueue_projection(projection['receive_seq'],delivery,
        now=datetime.now(timezone.utc).isoformat())
    with storage.db_connection() as db:
        return dict(db.execute('SELECT * FROM hook_delivery_outbox WHERE event_uuid=?',(event,)).fetchone())


@pytest.mark.parametrize('submitted',[False,True])
@pytest.mark.parametrize('rounds',[1,2])
def test_original_outbox_and_late_voice_finish_after_transfer_and_restart(identity_client,monkeypatch,tmp_path,submitted,rounds):
    monkeypatch.setattr(storage,'APP_DIR',tmp_path)
    monkeypatch.setattr(storage,'DB_FILE',tmp_path/'worker_client.sqlite3')
    w,t,p,b=bound(); originals=[]; voices=[]; bindings=[]
    for _ in range(rounds):
        value=media_coverage(b)
        result,_=upload(w,p,b,value,monkeypatch)
        assert result.status_code==200,result.text
        mid=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
        voices.append(voice_payload(w,p,b,mid))
        originals.append(queued(w,p,b));bindings.append(b)
        _,_,_,_,lead,tid,prep,url,payload=successor(monkeypatch,submitted=submitted,existing=(w,t,p,b))
        assert client.post(url,json=payload).status_code==200
        result=post(w,f"/workers/{w['id']}/wechat/native-bindings",body({'id':tid},p,prep))
        assert result.status_code==200,result.text
        b=result.json()['data'];t={'id':tid}
    # A restart changes process generation, never the queued envelope or owner.
    current={**p,'process_generation':'process-2'}
    assert heartbeat(w,context(**current)).status_code==200
    assert post(w,f"/workers/{w['id']}/run-status",{'client_instance_id':w['instance'],'run_status':'paused'}).status_code==200
    inv=post(w,f"/workers/{w['id']}/wechat/native-work",current).json()['data']
    for original in originals:
        assert any(x['binding_id']==original['binding_id'] and x['binding_revision']==original['binding_revision']
                   for x in inv['retired_evidence'])
    runtime=object.__new__(HookFormalRuntime)
    runtime.scope=SimpleNamespace(**current);runtime.account=current
    runtime.credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
    runtime.client=HookFormalClient(identity_client.base_url,5);runtime.identity_client=identity_client
    runtime.outbox=HookDeliveryOutbox(max_storage_bytes=1048576)
    for binding in inv['retired_evidence']:
        runtime.drain_pending(binding)
        runtime.drain_pending(binding) # Completed queues stay completed after another poll.
    for original in originals:
        with storage.db_connection() as db:
            stored=dict(db.execute('SELECT * FROM hook_delivery_outbox WHERE event_uuid=?',(original['event_uuid'],)).fetchone())
            assert stored['status']=='stored' and stored['attempt_count']==1
            for key in ('event_uuid','binding_id','binding_revision','payload_json','payload_digest','receive_seq'):
                assert stored[key]==original[key]
        envelope=shared_adapter('hook_contract_rules').loads(original['payload_json'])
        duplicate=post(w,f"/workers/{w['id']}/wechat/messages/native-observations",envelope)
        assert duplicate.status_code==200,duplicate.text
        with SessionLocal() as db:
            receipt=db.scalar(select(HookNativeReceipt).where(HookNativeReceipt.event_uuid==original['event_uuid']))
            assert receipt.payload_json==envelope and receipt.payload_digest==original['payload_digest']
            assert receipt.dispatch_status=='authorization_retired'
    for voice in voices:
        payload={**voice,'process_generation':current['process_generation'],'attempt_process_generation':p['process_generation']}
        result=post(w,f"/workers/{w['id']}/wechat/native-voice/result",payload)
        assert result.status_code==200,result.text
        assert result.json()['data']['disposition']=='authorization_retired'
        assert post(w,f"/workers/{w['id']}/wechat/native-voice/result",payload).json()['data']==result.json()['data']
    with SessionLocal() as db:
        for old in bindings:
            conversation=db.get(Conversation,old['conversation_id'])
            assert conversation.status=='closed' and not conversation.ai_enabled and conversation.next_recall_at is None
        assert len(list(db.scalars(select(HookVoiceReceipt))))==rounds
        assert not list(db.scalars(select(ReplyAction))) and not list(db.scalars(select(MessageBatch)))
        assert db.get(WechatSessionBinding,b['binding_id']).native_context['business_enabled'] is True
    assert post(w,f"/workers/{w['id']}/wechat/native-work",current).json()['data']['run_status']=='paused'


@pytest.mark.parametrize('case',['worker','instance','account','epoch','peer','revision','content','audit_gap'])
def test_archival_rejects_changed_scope_or_original_payload(monkeypatch,tmp_path,case):
    monkeypatch.setattr(storage,'APP_DIR',tmp_path)
    monkeypatch.setattr(storage,'DB_FILE',tmp_path/'worker_client.sqlite3')
    w,t,p,b=bound();original=queued(w,p,b)
    _,_,_,_,lead,tid,prep,url,payload=successor(monkeypatch,existing=(w,t,p,b))
    assert client.post(url,json=payload).status_code==200
    envelope=shared_adapter('hook_contract_rules').loads(original['payload_json'])
    endpoint=f"/workers/{w['id']}/wechat/messages/native-observations"
    if case=='content':
        assert post(w,endpoint,envelope).status_code==200
        envelope['messages'][0]['content']='must not replace original'
    elif case=='audit_gap':
        from app.models.audit import OperationLog
        with SessionLocal() as db:
            record=db.scalar(select(OperationLog).where(OperationLog.event_type=='lead_followup_revoked'))
            metadata=deepcopy(record.extra_metadata)
            metadata['bindings'][0]['worker_id']=str(uuid.uuid4())
            record.extra_metadata=metadata;db.commit()
        inv=post(w,f"/workers/{w['id']}/wechat/native-work",p).json()['data']
        assert not any(x['binding_id']==b['binding_id'] and x['binding_revision']==b['binding_revision'] for x in inv['retired_evidence'])
    else:
        field,value={'worker':('worker_id',str(uuid.uuid4())),'instance':('client_instance_id','another-instance'),
            'account':('self_wxid','wxid_other_account'),'epoch':('account_epoch',str(uuid.uuid4())),
            'peer':('peer_wxid','wxid_other_peer'),'revision':('binding_revision',99)}[case]
        envelope[field]=value
        if case=='account':envelope['messages'][0]['to_wxid']=value
        if case=='peer':envelope['messages'][0]['from_wxid']=value
        envelope['event_uuid']=shared_adapter('hook_contract_rules').delivery_id(envelope['source_event_uuid'],
            envelope['worker_id'],envelope['client_instance_id'],envelope['account_epoch'],envelope['binding_id'],envelope['binding_revision'])
    response=post(w,endpoint,envelope)
    assert response.status_code in {409,422},response.text
    with SessionLocal() as db:
        assert len(list(db.scalars(select(HookNativeReceipt))))==(1 if case=='content' else 0)
        assert not list(db.scalars(select(ReplyAction)))
