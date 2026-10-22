"""Synthetic native history through real HTTP; all notifications use a fake adapter."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.contracts.shared_rules import shared_adapter
from app.models.c3 import Conversation, HandoffEvent, ReplyAction, SentAck
from app.models.sales import Sales
from app.models.worker import Worker
from app.models.wechat import WechatSessionBinding
from app.services import feishu_service, hook_history_service
from test_hook_business_http import clean, post
from test_hook_history_business_http import bound, coverage, upload
from test_hook_native_send_http import beginning

NOTICE = '测试客户开启了朋友验证，你还不是他（她）朋友。请先发送朋友验证请求，对方验证通过后，才能聊天。<a href="weixin://findfriend/verifycontact">发送朋友验证</a>'
REASON = 'HOOK_FRIEND_VERIFICATION_REQUIRED'


def append(value, *, kind='10000', sender='0', text=NOTICE, second=None):
    for scan in value['scans']:
        d = scan['databases'][0]; rows = d['pages'][0]; last = rows[-1]
        rows.append({**last, 'localId':str(int(last['localId'])+1),
            'MsgSvrID':str(int(last['MsgSvrID'])+1), 'Type':kind, 'IsSender':sender,
            'StrContent':text, 'CreateTime':str(second) if second else last['CreateTime'],
            'Sequence':str(int(last['Sequence'])+1), 'MsgSequence':str(int(last['MsgSequence'])+1)})
        d.update(upper_local_id=rows[-1]['localId'],row_count=str(len(rows)),count_after=str(len(rows)))


def notifications(w, monkeypatch):
    sent=[]
    with SessionLocal() as db:
        sale=db.scalar(select(Sales).where(Sales.worker_id==w['id']))
        sale.feishu_user_id='ou_synthetic_recipient';db.commit()
    monkeypatch.setattr(feishu_service,'get_feishu_adapter',lambda:SimpleNamespace(send_text_message=lambda to,text:sent.append((to,text))))
    return sent


@pytest.mark.parametrize('kind,sender,text,expected',[
    ('10000','0',NOTICE,True),('10000','0',NOTICE.replace('）朋友','）的朋友'),True),
    ('1','0',NOTICE,False),('10000','1',NOTICE,False),
    ('10000','0','我听说开启了朋友验证，怎么回事？',False),
    ('10000','0','引用：'+NOTICE+'这不是真的',False),
])
def test_exact_system_notice_only(kind,sender,text,expected):
    rule=shared_adapter('hook_history_rules')
    assert rule.is_friend_verification_required_notice({'Type':kind,'IsSender':sender,'StrContent':text}) is expected


def test_customer_handoff_once_without_stopping_worker_or_reopening_closed_event(monkeypatch):
    w,_,p,b=bound();sent=notifications(w,monkeypatch);value=coverage(b);append(value)
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    assert r.json()['data']['history_ready'] and r.json()['data']['message_batch'] is None
    with SessionLocal() as db:
        event=db.scalar(select(HandoffEvent));assert event.handoff_reason_code==REASON
        assert event.notify_status=='succeeded'
        assert db.get(Worker,w['id']).run_status=='running'
        assert db.get(Conversation,b['conversation_id']).status=='waiting_sales_reply'
        event_id=event.id
    assert len(sent)==1 and sent[0][0]=='ou_synthetic_recipient' and '好友验证' in sent[0][1]
    for _ in range(2):
        r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200
    append(value,kind='1',sender='1',text='销售已确认好友关系并处理')
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:
        assert db.get(HandoffEvent,event_id).closed_at is not None
        assert db.get(Conversation,b['conversation_id']).status=='sales_replied_waiting_user'
    # A new DB handle/process generation changes the catalog fingerprint only.
    for scan in value['scans']:
        scan['databases'][0]['generation']='b'*64
        scan['catalog_fingerprint']=shared_adapter('hook_contract_rules').digest([['MSG0.db','b'*64]])
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200
    with SessionLocal() as db:
        assert len(list(db.scalars(select(HandoffEvent))))==1
        assert db.get(HandoffEvent,event_id).closed_at is not None
    assert len(sent)==1
    append(value,kind='1',sender='0',text='请继续介绍车辆配置')
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    assert r.json()['data']['message_batch'] is not None


def test_older_human_reply_discovered_later_cannot_close_new_notice(monkeypatch):
    w,_,p,b=bound();sent=notifications(w,monkeypatch);value=coverage(b)
    append(value,kind='1',sender='1',text='这是此前的人工回复')
    append(value)
    initial=deepcopy(value)
    for scan in initial['scans']:
        database=scan['databases'][0];database['pages'][0].pop(-2)
        database.update(row_count=str(len(database['pages'][0])),count_after=str(len(database['pages'][0])))
    r,_=upload(w,p,b,initial,monkeypatch);assert r.status_code==200,r.text
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:
        assert db.scalar(select(HandoffEvent)).closed_at is None
        assert db.get(Conversation,b['conversation_id']).status=='waiting_sales_reply'
    append(value,kind='1',sender='1',text='现在已处理好友验证')
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:assert db.scalar(select(HandoffEvent)).closed_at is not None
    assert len(sent)==1


def test_acceptance_notice_alone_does_not_close_existing_handoff(monkeypatch):
    w,_,p,b=bound();notifications(w,monkeypatch);value=coverage(b);append(value)
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200
    append(value,kind='1',sender='0',text='我通过了你的朋友验证请求，现在我们可以开始聊天了')
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200
    with SessionLocal() as db:assert db.scalar(select(HandoffEvent)).closed_at is None


@pytest.mark.parametrize('bad_order',['missing','conflicting'])
def test_notice_pagination_position_cannot_replace_business_order(bad_order,monkeypatch):
    w,_,p,b=bound();notifications(w,monkeypatch);value=coverage(b);append(value)
    for scan in value['scans']:
        row=scan['databases'][0]['pages'][0][-1]
        row['MsgSequence']='0' if bad_order=='missing' else '1'
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200
    append(value,kind='1',sender='1',text='人工回复')
    for scan in value['scans']:
        rows=scan['databases'][0]['pages'][0]
        rows[-1]['MsgSequence']=str(int(rows[1]['MsgSequence'])+2)
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:
        assert db.scalar(select(HandoffEvent)).closed_at is None
        assert db.get(Worker,w['id']).run_status=='running'


def test_missing_notice_order_proof_cannot_close_handoff(monkeypatch):
    w,_,p,b=bound();notifications(w,monkeypatch);value=coverage(b);append(value)
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200
    # Simulate a complete current scan that no longer contains the original notice.
    for scan in value['scans']:
        database=scan['databases'][0];database['pages'][0].pop()
        last=database['pages'][0][-1]
        database.update(upper_local_id=last['localId'],row_count='2',count_after='2')
    append(value,kind='1',sender='1',text='人工回复但原提示顺序证据缺失')
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:
        assert db.scalar(select(HandoffEvent)).closed_at is None
        assert db.get(Conversation,b['conversation_id']).status=='waiting_sales_reply'


@pytest.mark.parametrize('invalid',['wrong_peer','changed_scan','unordered'])
def test_unverified_history_cannot_handoff(invalid,monkeypatch):
    w,_,p,b=bound();sent=notifications(w,monkeypatch);value=coverage(b);append(value)
    if invalid=='wrong_peer':
        for scan in value['scans']:scan['databases'][0]['pages'][0][-1]['StrTalker']='wxid_other_customer'
    elif invalid=='changed_scan':value['scans'][1]['databases'][0]['pages'][0][-1]['StrContent']='changed'
    else:
        for scan in value['scans']:
            rows=scan['databases'][0]['pages'][0];rows[1]['MsgSequence']=rows[0]['MsgSequence']
    r,_=upload(w,p,b,value,monkeypatch)
    assert r.status_code==(200 if invalid=='unordered' else 409),r.text
    with SessionLocal() as db:assert db.scalar(select(HandoffEvent)) is None
    assert not sent


@pytest.mark.parametrize('restart',['running','paused','faulted','backend_timeout'])
def test_original_send_settles_through_history_and_releases_flow(restart,monkeypatch):
    w,p,b,h,permit,value,path,now,start=beginning(monkeypatch);sent=notifications(w,monkeypatch)
    second=int(b['history_since_create_time'])+40
    append(value,kind='1',sender='1',text=permit['reply_text'],second=second)
    append(value,second=second)
    if restart=='backend_timeout':
        for scan in value['scans']:
            notice=scan['databases'][0]['pages'][0][-1]
            notice['Sequence']=str(int(notice['Sequence'])+10)
            notice['MsgSequence']=str(int(notice['MsgSequence'])+10)
    for scan in value['scans']:scan['observed_at']=now(50)
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:datetime.fromisoformat(now(60)))
    with SessionLocal() as db:
        original=deepcopy(db.get(ReplyAction,permit['reply_action_id']).ai_payload)
    expected_run='faulted' if restart=='backend_timeout' else restart
    if restart=='backend_timeout':
        from app.services import c3_service
        with SessionLocal() as db:
            assert c3_service.recover_stale_sending_reply_action(db,reply_action_id=permit['reply_action_id'],now=datetime.fromisoformat(now(600)))
            db.commit();feishu_service.run_post_commit_effects(db)
    if expected_run!='running':
        r=post(w,f"/workers/{w['id']}/run-status",{'run_status':expected_run,'client_instance_id':w['instance']});assert r.status_code==200
    settle={**p,'task_id':permit['task_id'],'send_token':permit['send_token'],
        'journal_sha256':start['journal_sha256'],'completed_at':now(41),'call_completed':True}
    reconciliation={**{k:v for k,v in settle.items() if k not in {'completed_at','call_completed'}},'reply_action_id':permit['reply_action_id'],'flow_id':permit['flow_id'],
        'lease_fencing_token':permit['lease_fencing_token']}
    # The authenticated original-flow recovery uses the same full coverage route.
    body={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],
        'capture_id':str(uuid.uuid4()),'coverage':value,'coverage_digest':shared_adapter('hook_contract_rules').digest(value),
        'reconciliation':reconciliation}
    r=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",body);assert r.status_code==200,r.text
    r=post(w,path+'/settle',settle,h);assert r.status_code==200,r.text
    assert r.json()['data']['local_send_observed'] and not r.json()['data']['recipient_delivery_confirmed']
    assert not r.json()['data']['automatic_resend_allowed']
    assert post(w,path+'/settle',settle,h).json()['data']==r.json()['data']
    with SessionLocal() as db:
        action=db.get(ReplyAction,permit['reply_action_id'])
        assert action.ai_payload['native_send_identity']==original['native_send_identity']
        assert action.ai_payload['native_send_attempt']==original['native_send_attempt']
        assert db.scalar(select(SentAck)).send_result==('unknown' if restart=='backend_timeout' else 'sent')
        if restart=='backend_timeout':assert action.status=='unknown_send_result'
        assert db.get(Conversation,b['conversation_id']).status=='waiting_sales_reply'
        assert db.scalar(select(HandoffEvent)).closed_at is None
        assert db.get(Worker,w['id']).run_status==expected_run
    assert len(sent)==1
    r=post(w,f"/workers/{w['id']}/inflight-flow/finish",{'flow_id':permit['flow_id'],'terminal_kind':'task_terminal','conversation_id':b['conversation_id']},h)
    assert r.status_code==200,r.text
    with SessionLocal() as db:assert not db.get(Worker,w['id']).inflight_flow_state
    if expected_run=='faulted':
        r=post(w,f"/workers/{w['id']}/run-status",{'run_status':'running','client_instance_id':w['instance'],'recover_from_fault':True})
        assert r.status_code==200,r.text
    if restart=='backend_timeout':
        append(value,kind='1',sender='1',text='旧人工回复',second=second)
        for scan in value['scans']:
            rows=scan['databases'][0]['pages'][0]
            rows[-1]['Sequence']=str(int(rows[-2]['Sequence'])-1)
            rows[-1]['MsgSequence']=str(int(rows[-2]['MsgSequence'])-1)
        body.pop('reconciliation')
        body.update(capture_id=str(uuid.uuid4()),coverage_digest=shared_adapter('hook_contract_rules').digest(value))
        r=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",body);assert r.status_code==200,r.text
        with SessionLocal() as db:
            event=db.scalar(select(HandoffEvent))
            assert event.handoff_reason_code=='SEND_ACK_TIMEOUT' and event.closed_at is None
        append(value,kind='1',sender='1',text='现在已确认并处理好友验证',second=second)
        for scan in value['scans']:
            rows=scan['databases'][0]['pages'][0]
            rows[-1]['Sequence']=str(int(rows[-3]['Sequence'])+1)
            rows[-1]['MsgSequence']=str(int(rows[-3]['MsgSequence'])+1)
        body.update(capture_id=str(uuid.uuid4()),coverage_digest=shared_adapter('hook_contract_rules').digest(value))
        r=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",body);assert r.status_code==200,r.text
        with SessionLocal() as db:
            assert db.scalar(select(HandoffEvent)).closed_at is not None
            assert db.get(ReplyAction,permit['reply_action_id']).status=='unknown_send_result'
            assert db.scalar(select(SentAck)).send_result=='unknown'
        assert len(sent)==1
    if expected_run!='paused':
        # Same sales/Worker can pull the next customer's actual invite task.
        from test_hook_account_http import client
        lead=client.post('/api/leads',json={'customer_name':'Synthetic next customer','phones':['13800000004']})
        assert lead.status_code==200,lead.text
        result=client.get(f"/api/workers/{w['id']}/tasks/pull",headers={
            'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance']})
        assert result.status_code==200,result.text
        assert result.json()['data']['task']['business_object']['lead']['id']==lead.json()['data']['id']


@pytest.mark.parametrize('resolved_by',['acceptance','human'])
def test_old_notice_already_resolved_by_later_verified_event_does_not_reopen(resolved_by,monkeypatch):
    w,_,p,b=bound();sent=notifications(w,monkeypatch);value=coverage(b);append(value)
    if resolved_by=='acceptance':
        append(value,kind='1',sender='0',text='我通过了你的朋友验证请求，现在我们可以开始聊天了')
    else:
        append(value,kind='1',sender='1',text='销售已人工处理')
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:assert db.scalar(select(HandoffEvent)) is None
    assert not sent


@pytest.mark.parametrize('count',[0,2])
def test_notice_never_substitutes_for_missing_or_ambiguous_send_proof(count,monkeypatch):
    w,p,b,h,permit,value,path,now,start=beginning(monkeypatch);sent=notifications(w,monkeypatch)
    second=int(b['history_since_create_time'])+40
    for _ in range(count):append(value,kind='1',sender='1',text=permit['reply_text'],second=second)
    append(value,second=second)
    for scan in value['scans']:scan['observed_at']=now(50)
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:datetime.fromisoformat(now(60)))
    body={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],
        'capture_id':str(uuid.uuid4()),'coverage':value,'coverage_digest':shared_adapter('hook_contract_rules').digest(value)}
    r=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",body);assert r.status_code==200,r.text
    payload={**p,'task_id':permit['task_id'],'send_token':permit['send_token'],
        'journal_sha256':start['journal_sha256'],'completed_at':now(41),'call_completed':True}
    r=post(w,path+'/observe',payload,h);assert r.status_code==200,r.text
    assert not r.json()['data']['unique_local_send_observed']
    assert not r.json()['data']['deadline_elapsed']
    with SessionLocal() as db:
        assert db.get(ReplyAction,permit['reply_action_id']).status=='sending'
        assert db.scalar(select(SentAck)) is None
        assert db.get(Worker,w['id']).inflight_flow_state
        assert db.scalar(select(HandoffEvent)).handoff_reason_code==REASON
    assert len(sent)==1


@pytest.mark.parametrize('first_reason',['image','friend'])
def test_reused_handoff_keeps_both_restriction_boundaries(first_reason,monkeypatch):
    from test_hook_native_media import media_coverage
    w,_,p,b=bound();sent=notifications(w,monkeypatch)
    value=media_coverage(b,'3') if first_reason=='image' else coverage(b)
    value['schema_version']='hook-history-coverage-v2'
    if first_reason=='friend':append(value)
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:
        event=db.scalar(select(HandoffEvent));event_id=event.id
        first_evidence=set(event.evidence_refs);first_code=event.handoff_reason_code
    if first_reason=='image':append(value)
    else:append(value,kind='3',text='<msg><img md5="test-only-later"/></msg>')
    for scan in value['scans']:
        row=scan['databases'][0]['pages'][0][-1]
        row['Sequence']=str(int(row['Sequence'])+10)
        row['MsgSequence']=str(int(row['MsgSequence'])+10)
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    append(value,kind='1',sender='1',text='只处理过此前的问题')
    for scan in value['scans']:
        rows=scan['databases'][0]['pages'][0]
        rows[-1]['Sequence']=str(int(rows[-2]['Sequence'])-1)
        rows[-1]['MsgSequence']=str(int(rows[-2]['MsgSequence'])-1)
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:
        event=db.get(HandoffEvent,event_id)
        assert event.closed_at is None and event.handoff_reason_code==first_code
        assert first_evidence.issubset(event.evidence_refs)
        assert event.ai_payload['native_friend_verification_notice_keys']
        assert len(list(db.scalars(select(HandoffEvent))))==1
    append(value,kind='1',sender='1',text='现在已处理完两个问题')
    for scan in value['scans']:
        rows=scan['databases'][0]['pages'][0]
        rows[-1]['Sequence']=str(int(rows[-3]['Sequence'])+1)
        rows[-1]['MsgSequence']=str(int(rows[-3]['MsgSequence'])+1)
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:assert db.get(HandoffEvent,event_id).closed_at is not None
    append(value,kind='1',sender='0',text='继续咨询车辆')
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    assert r.json()['data']['message_batch'] is not None
    assert len(sent)==1


@pytest.mark.parametrize('damage',[None,'other_peer','incomplete','changed_scan','ordinary_text','quoted_notice','unresolved_order'])
def test_zero_outgoing_notice_does_not_relax_verified_restriction_boundary(monkeypatch,damage):
    w,_,p,b=bound();sent=notifications(w,monkeypatch);value=coverage(b)
    append(value,kind='1',sender='1',text='synthetic outgoing')
    append(value)
    for scan in value['scans']:
        d=scan['databases'][0];rows=d['pages'][0];rows[-2]['MsgSvrID']='0'
        if damage=='other_peer':rows[-1]['StrTalker']='wxid_other'
        if damage=='incomplete':d['row_count']=d['count_after']='99'
        if damage=='ordinary_text':rows[-1]['Type']='1'
        if damage=='quoted_notice':rows[-1]['StrContent']='引用：'+NOTICE+'这不是真的'
        if damage=='unresolved_order':rows[1]['Sequence']=rows[0]['Sequence']
    if damage=='changed_scan':value['scans'][1]['databases'][0]['pages'][0][-1]['StrContent']='changed'
    result,_=upload(w,p,b,value,monkeypatch)
    if damage in {'other_peer','incomplete','changed_scan'}:
        assert result.status_code==409
    else:
        assert result.status_code==200,result.text
        assert not result.json()['data']['history_ready']
        assert result.json()['data']['message_batch'] is None
    with SessionLocal() as db:
        events=list(db.scalars(select(HandoffEvent)))
        assert any(e.handoff_reason_code==REASON for e in events) is (damage is None)
        assert db.get(Worker,w['id']).run_status=='running'
    assert len(sent)<=1


@pytest.mark.parametrize('blocker',['callback','voice'])
def test_pending_outgoing_does_not_mask_other_notice_admission_blockers(monkeypatch,blocker):
    w,_,p,b=bound();notifications(w,monkeypatch);value=coverage(b)
    append(value,kind='1',sender='1',text='synthetic outgoing');append(value)
    for scan in value['scans']:scan['databases'][0]['pages'][0][-2]['MsgSvrID']='0'
    if blocker=='callback':
        from test_hook_business_http import native_payload
        payload=native_payload(w,b,mid='8000000000000000999',text='not yet covered')
        r=post(w,f"/workers/{w['id']}/wechat/messages/native-observations",payload)
        assert r.status_code==200,r.text
    else:
        append(value,kind='34',text='<msg><voicemsg voicelength="1000"/></msg>')
    r,_=upload(w,p,b,value,monkeypatch)
    assert r.status_code==200,r.text
    assert not r.json()['data']['history_ready']
    with SessionLocal() as db:
        assert not any(e.handoff_reason_code==REASON for e in db.scalars(select(HandoffEvent)))
