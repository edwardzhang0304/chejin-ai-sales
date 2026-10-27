"""Original segmented reply refusal recovery; real local HTTP/SQLite, synthetic model/Hook."""
from datetime import datetime, timedelta, timezone
import time
import pytest
from sqlalchemy import select
from test_hook_customer_refusal_closure import (
    case, clean, identity_client, lab, provider_success, restart_dispatch,
    HookFormalClient, ApiError, SessionLocal, ReplyAction, Task, Worker,
)
from test_reply_sequence_http import SequenceModel
from app.services import c3_service

@pytest.fixture(autouse=True)
def local_admin():
    from fastapi import Request
    from app.core.auth import require_admin_auth
    from app.main import app
    def auth(request: Request):
        request.state.auth_actor={"operator_id":"00000000-0000-0000-0000-000000000001",
            "operator_name":"Synthetic audit","actor_type":"admin_account","session_id":"test-session"}
    app.dependency_overrides[require_admin_auth]=auth
    yield
    app.dependency_overrides.pop(require_admin_auth,None)

@pytest.fixture
def sequence_case(monkeypatch,request):
    monkeypatch.setattr(c3_service,"get_ai_engine_adapter",SequenceModel)
    return request.getfixturevalue("case")

def interrupt_segment(c,monkeypatch,loss,reason,segment):
    with SessionLocal() as db:
        actions=list(db.scalars(select(ReplyAction).where(ReplyAction.batch_id==c.data["batch_id"]).order_by(ReplyAction.segment_index)))
        assert len(actions)==3
    time.sleep(1.05)
    provider_success(c)
    first=c.rt.reply_once(c.binding,c.data,history=c.history)
    assert first["status"]=="sent"
    flow=c.rt.active_flow_id
    assert flow==c.data["task"]["id"]
    history=c.rt.sync(c.binding)
    second={**c.rt.batch_view(c.data["batch_id"]),"batch_id":c.data["batch_id"]}
    if segment==3:
        assert c.rt.reply_once(c.binding,second,history=history)['status']=='sent'
        history=c.rt.sync(c.binding)
        second={**c.rt.batch_view(c.data['batch_id']),'batch_id':c.data['batch_id']}
    assert second["reply_action"]["segment_index"]==segment
    original=HookFormalClient.call
    refused=lost=False
    def call(client,credentials,method,path,body=None,**kw):
        nonlocal refused,lost
        if path.endswith("/native-replies/"+second["reply_action"]["id"]+"/claim") and not refused:
            refused=True
            with SessionLocal() as db:
                if reason=="expiry":
                    db.get(ReplyAction,second["reply_action"]["id"]).expire_at=datetime.now(timezone.utc)-timedelta(seconds=1)
                else:
                    from app.models.wechat import WechatSessionBinding
                    db.get(WechatSessionBinding,c.binding["binding_id"]).allow_listening=False
                db.commit()
        should_lose=path.endswith("/inflight-flow/finish" if loss=="finish" else "/native-recovery") and not lost
        if should_lose and loss=="request":
            lost=True
            raise ApiError("HOOK_BACKEND_NETWORK_FAILED","synthetic request loss",0)
        result=original(client,credentials,method,path,body,**kw)
        if should_lose:
            lost=True
            raise ApiError("HOOK_BACKEND_NETWORK_FAILED","synthetic reply loss",0)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(HookFormalClient,"call",call)
        with pytest.raises(ApiError,match="synthetic"):
            c.rt.reply_once(c.binding,second,history=history)
    assert refused and lost
    return second


@pytest.mark.parametrize("segment",[2,3])
@pytest.mark.parametrize("reason",["expiry","customer_pause"])
@pytest.mark.parametrize("loss",["request","response","finish"])
def test_second_segment_unpermitted_recovery(sequence_case,monkeypatch,loss,reason,segment):
    c=sequence_case
    second=interrupt_segment(c,monkeypatch,loss,reason,segment)
    from test_hook_business_http import post
    assert post(c.w,f"/workers/{c.w['id']}/run-status",{"client_instance_id":c.w["instance"],"run_status":"paused"}).status_code==200
    evidence={p:p.read_bytes() for p in c.rt.private_root.rglob('*.json')
        if p.name in {'permit.json','intent.json','trigger.json','outcome.json','request.json'}}
    restart_dispatch(c)
    assert all(p.read_bytes()==raw for p,raw in evidence.items())
    assert sum(row[0]=="/SendTextMsg" for row in c.server.requests)==segment-1
    with SessionLocal() as db:
        t=db.get(Task,second["task"]["id"])
        assert t.status in {"cancelled","failed"},t.status
        assert not db.get(Worker,c.w["id"]).inflight_flow_state.get("flow_id")

@pytest.mark.parametrize("boundary",["update","retention"])
def test_paused_delivery_new_revision_closes_old_lifecycle(case,boundary):
    from test_hook_customer_refusal_closure import callback
    from chejin_worker_client import storage
    from chejin_worker_client.hook_retention import retire_inbox
    from chejin_worker_client.update_safety import business_wait_reason
    from app.models.wechat import WechatSessionBinding
    from app.services.wechat_service import _set_binding_state
    c=case
    callback(c)
    def change(enabled):
        with SessionLocal() as db:
            b=db.get(WechatSessionBinding,c.binding['binding_id'])
            _set_binding_state(b,status='bound',listen_status='listening' if enabled else 'paused',
                allow_listening=enabled,preserve_lead=True,remark_code=b.remark_code)
            db.commit()
    change(False)
    with pytest.raises(ApiError) as err:c.rt.drain(c.binding)
    assert err.value.code=='HOOK_BINDING_PAUSED'
    with storage.db_connection() as db:
        old=dict(db.execute('SELECT * FROM hook_delivery_outbox').fetchone())
        original_raw=db.execute('SELECT raw_body FROM hook_raw_inbox').fetchone()[0]
    change(True)
    inventory=c.rt.client.call(c.rt.credentials,'POST',f'/workers/{c.w["id"]}/wechat/native-work',c.rt.account)
    current=next(b for b in inventory['bindings'] if b['binding_id']==c.binding['binding_id'])
    assert current['binding_revision']>c.binding['binding_revision']
    c.rt.drain(current)
    with storage.db_connection() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM hook_delivery_outbox ORDER BY binding_revision')]
        assert rows[-1]['status']=='stored'
        assert rows[0]['payload_json']==old['payload_json']
        assert db.execute('SELECT raw_body FROM hook_raw_inbox').fetchone()[0]==original_raw
    print({'boundary':boundary,'deliveries':[(r['binding_revision'],r['status']) for r in rows]})
    if boundary=='update':
        snapshot=storage.hook_update_business_blockers(process_generation=c.rt.scope.process_generation)
        print({'snapshot':snapshot,'update_reason':business_wait_reason(snapshot)})
        assert not business_wait_reason(snapshot)[0], 'Old revision has no reachable settlement and blocks future update'
    else:
        result=retire_inbox(c.rt.private_root,retention_days=1,now=datetime.now(timezone.utc)+timedelta(days=2))
        print(result)



@pytest.mark.parametrize('field',['flow_id','reply_action_id','lease_fencing_token','account_epoch'])
def test_segment_restart_rejects_changed_original_request(sequence_case,monkeypatch,field):
    import json,uuid
    c=sequence_case
    second=interrupt_segment(c,monkeypatch,'request','customer_pause',2)
    p=c.rt.private_root/'recovery'/'send_unpermitted'/second['task']['id']/'request.json'
    saved=json.loads(p.read_bytes())
    saved[field]=99 if field=='lease_fencing_token' else str(uuid.uuid4())
    p.write_text(json.dumps(saved))
    with pytest.raises(ValueError,match='HOOK_RECOVERY_(IDENTITY_MISMATCH|INTENT_CONFLICT)'):
        restart_dispatch(c)
    with SessionLocal() as db:
        assert db.get(Worker,c.w['id']).inflight_flow_state.get('flow_id')
    assert sum(r[0]=='/SendTextMsg' for r in c.server.requests)==1
    assert not p.with_name('receipt.json').exists()


def test_segment_restart_does_not_replay_unrelated_local_request(sequence_case,monkeypatch):
    import uuid
    c=sequence_case
    interrupt_segment(c,monkeypatch,'request','customer_pause',2)
    p=c.rt.private_root/'recovery'/'send_unpermitted'/str(uuid.uuid4())/'request.json'
    p.parent.mkdir(parents=True);p.write_text('{"unrelated":"must not replay"}')
    original=p.read_bytes()
    restart_dispatch(c)
    assert p.read_bytes()==original and not p.with_name('receipt.json').exists()
    assert sum(r[0]=='/SendTextMsg' for r in c.server.requests)==1


@pytest.mark.parametrize('pause_at',['store','dispatch'])
@pytest.mark.parametrize('loss',['store_reply','dispatch_reply'])
def test_paused_delivery_restart_requires_real_current_dispatch(case,monkeypatch,pause_at,loss,tmp_path):
    from test_hook_customer_refusal_closure import callback
    from chejin_worker_client import storage
    from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
    from chejin_worker_client.hook_retention import retire_inbox
    from app.models.wechat import WechatSessionBinding,MessageEvent
    from app.services.wechat_service import _set_binding_state
    c=case;callback(c)
    def change(enabled):
        with SessionLocal() as db:
            b=db.get(WechatSessionBinding,c.binding['binding_id'])
            _set_binding_state(b,status='bound',listen_status='listening' if enabled else 'paused',
                allow_listening=enabled,preserve_lead=True,remark_code=b.remark_code)
            db.commit()
    original=HookFormalClient.call
    if pause_at=='store':change(False)
    def call(client,credentials,method,path,body=None,**kw):
        if pause_at=='dispatch' and path.endswith('/dispatch'):change(False)
        return original(client,credentials,method,path,body,**kw)
    with monkeypatch.context() as p:
        p.setattr(HookFormalClient,'call',call)
        with pytest.raises(ApiError):c.rt.drain(c.binding)
    with storage.db_connection() as db:
        old=dict(db.execute('SELECT * FROM hook_delivery_outbox').fetchone())
    change(True)
    inventory=c.rt.client.call(c.rt.credentials,'POST',f'/workers/{c.w["id"]}/wechat/native-work',c.rt.account)
    current=next(b for b in inventory['bindings'] if b['binding_id']==c.binding['binding_id'])
    original_store=type(c.rt.identity_client).store_observations
    def store(client,*args,**kw):
        result=original_store(client,*args,**kw)
        if loss=='store_reply':raise ApiError('HOOK_BACKEND_NETWORK_FAILED','synthetic stored reply loss',0)
        return result
    def lost_dispatch(client,credentials,method,path,body=None,**kw):
        result=original(client,credentials,method,path,body,**kw)
        if path.endswith('/dispatch'):raise ApiError('HOOK_BACKEND_NETWORK_FAILED','synthetic dispatch reply loss',0)
        return result
    with monkeypatch.context() as p:
        p.setattr(type(c.rt.identity_client),'store_observations',store)
        if loss=='dispatch_reply':p.setattr(HookFormalClient,'call',lost_dispatch)
        with pytest.raises(ApiError):c.rt.drain(current)
    assert storage.hook_update_business_blockers(c.rt.scope.process_generation)['hook_pending_delivery']==2
    # Advance lease time only; no fake stored acknowledgement or changed payload.
    import chejin_worker_client.hook_formal_runtime as runtime_module
    future=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat()
    monkeypatch.setattr(runtime_module,'utc_now_iso',lambda:future)
    previous=c.rt
    c.rt=HookFormalRuntime(previous.client,previous.credentials,previous.gateway,previous.prepared,previous.private_root,wait=lambda _:None)
    c.rt.drain(current)
    assert storage.hook_update_business_blockers(c.rt.scope.process_generation)['hook_pending_delivery']==0
    with storage.db_connection() as db:
        saved=dict(db.execute('SELECT * FROM hook_delivery_outbox WHERE event_uuid=?',(old['event_uuid'],)).fetchone())
        assert saved['payload_json']==old['payload_json'] and saved['status']=='covered_by_new_authorization'
    c.rt.drain(current)
    with SessionLocal() as db:
        events=list(db.scalars(select(MessageEvent).where(MessageEvent.conversation_id==c.binding['conversation_id'])))
        native_ids=[(e.raw_payload or {}).get('native_msg_id') for e in events]
        assert len(native_ids)==len(set(native_ids))
    from chejin_worker_client.diagnostic_bundle import export_bundle
    import zipfile,json
    export_bundle(tmp_path,tmp_path/'coverage.zip')
    with zipfile.ZipFile(tmp_path/'coverage.zip') as z:
        content=[z.read(n).decode() for n in z.namelist() if n.endswith('state/hook_delivery_outbox.json')]
        assert any('hook-delivery-coverage-v1' in s for s in content)
    assert retire_inbox(c.rt.private_root,retention_days=1,now=datetime.now(timezone.utc)+timedelta(days=2))['retired_rows']==1
    assert not any(r[0]=='/SendTextMsg' for r in c.server.requests)


@pytest.mark.parametrize('restored',[False,True])
@pytest.mark.parametrize('kind',['duplicate','multiple','conflict'])
@pytest.mark.parametrize('lose_reply',[False,True])
def test_duplicate_same_native_message_is_one_fact_and_can_close_delivery(case,restored,kind,lose_reply,monkeypatch):
    import json
    from chejin_worker_client.hook_inbox import NativeInbox
    from chejin_worker_client import storage
    from app.models.wechat import WechatSessionBinding
    from app.services.wechat_service import _set_binding_state
    c=case;scope=c.rt.scope
    message={'msgsvrid':'9000000000000000010','msgtype':'1','fromtype':'1',
        'fromid':c.binding['peer_wxid'],'toid':scope.self_wxid,
        'msg':'synthetic repeated callback item','time':datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    second=dict(message)
    if kind=='multiple':second['msgsvrid']='9000000000000000011'
    if kind=='conflict':second['msg']='synthetic conflicting text'
    raw=json.dumps({'ServerPort':str(scope.hook_api_port),'selfwxid':scope.self_wxid,
        'sendorrecv':'2','msgnumber':'2','msglist':[message,second]}).encode()
    NativeInbox(max_body_bytes=65536,max_inbox_bytes=1048576).persist(raw,scope)
    def change(enabled):
        with SessionLocal() as db:
            b=db.get(WechatSessionBinding,c.binding['binding_id'])
            _set_binding_state(b,status='bound',listen_status='listening' if enabled else 'paused',
                allow_listening=enabled,preserve_lead=True,remark_code=b.remark_code);db.commit()
    current=c.binding
    if restored:
        change(False)
        with pytest.raises(ApiError):c.rt.drain(current)
        change(True)
        inventory=c.rt.client.call(c.rt.credentials,'POST',f'/workers/{c.w["id"]}/wechat/native-work',c.rt.account)
        current=next(b for b in inventory['bindings'] if b['binding_id']==c.binding['binding_id'])
    if kind=='conflict':
        with pytest.raises(ApiError) as err:c.rt.drain(current)
        assert err.value.code=='HOOK_MESSAGE_ID_CONFLICT'
        assert storage.hook_update_business_blockers(scope.process_generation)['hook_pending_delivery']==(2 if restored else 1)
        with SessionLocal() as db:
            from app.models.wechat import MessageEvent
            facts=list(db.scalars(select(MessageEvent).where(MessageEvent.conversation_id==c.binding['conversation_id'])))
            assert not any((m.raw_payload or {}).get('native_msg_id')==message['msgsvrid'] for m in facts)
        return
    if lose_reply:
        original=HookFormalClient.call
        def lost(client,credentials,method,path,body=None,**kw):
            result=original(client,credentials,method,path,body,**kw)
            if path.endswith('/dispatch'):raise ApiError('HOOK_BACKEND_NETWORK_FAILED','synthetic reply loss',0)
            return result
        with monkeypatch.context() as patch:
            patch.setattr(HookFormalClient,'call',lost)
            with pytest.raises(ApiError):c.rt.drain(current)
        assert storage.hook_update_business_blockers(scope.process_generation)['hook_pending_delivery']==(2 if restored else 1)
        import chejin_worker_client.hook_formal_runtime as runtime_module
        future=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat()
        monkeypatch.setattr(runtime_module,'utc_now_iso',lambda:future)
        previous=c.rt
        c.rt=runtime_module.HookFormalRuntime(previous.client,previous.credentials,previous.gateway,previous.prepared,previous.private_root,wait=lambda _:None)
    c.rt.drain(current)
    assert storage.hook_update_business_blockers(scope.process_generation)['hook_pending_delivery']==0
    with storage.db_connection() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM hook_delivery_outbox ORDER BY binding_revision')]
        assert rows[-1]['status']=='stored'
        assert len(json.loads(rows[-1]['payload_json'])['messages'])==2
        if restored:
            proof=json.loads(rows[0]['receipt_json'])
            assert len(proof['replacement_dispatch']['message_event_ids'])==(1 if kind=='duplicate' else 2)
        assert db.execute('SELECT raw_body FROM hook_raw_inbox').fetchone()[0]==raw
    assert not any(r[0]=='/SendTextMsg' for r in c.server.requests)
    from chejin_worker_client.hook_retention import retire_inbox
    assert retire_inbox(c.rt.private_root,retention_days=1,now=datetime.now(timezone.utc)+timedelta(days=2))['retired_rows']==1
