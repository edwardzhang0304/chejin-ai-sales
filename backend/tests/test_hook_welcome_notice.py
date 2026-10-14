"""Native Type=1 acceptance notice must use original welcome, not a customer batch."""
from copy import deepcopy
import uuid
import pytest
from sqlalchemy import select,func
from app.core.database import SessionLocal
from app.models.c3 import MessageBatch,ReplyAction
from app.models.hook_native import HookHistoryReceipt
from app.models.wechat import MessageEvent,WechatSessionBinding
from app.services import c3_service
from app.contracts.shared_rules import shared_adapter
from test_hook_business_http import clean
from test_hook_history_business_http import bound,coverage,upload

NOTICE='我通过了你的朋友验证请求，现在我们可以开始聊天了'


def with_rows(binding,texts):
 value=coverage(binding)
 for scan in value['scans']:
  d=scan['databases'][0];original=d['pages'][0][0]
  rows=[{**original,'localId':str(7801+i),'MsgSvrID':str(8000000000000000001+i),
         'Sequence':str(int(original['Sequence'])+i),'MsgSequence':str(int(original['MsgSequence'])+i),
         'StrContent':text} for i,text in enumerate(texts)]
  d.update(upper_local_id=rows[-1]['localId'],row_count=str(len(rows)),count_after=str(len(rows)),pages=[rows])
 return value


def test_acceptance_notice_creates_original_welcome_once_with_raw_evidence(monkeypatch):
 w,t,p,b=bound();value=with_rows(b,[NOTICE])
 first,_=upload(w,p,b,value,monkeypatch);assert first.status_code==200,first.text
 data=first.json()['data'];assert data['history_ready'] and data['message_event_ids']==[]
 with SessionLocal() as db:
  batch=db.get(MessageBatch,data['message_batch']['batch_id']);assert batch.trigger_type=='friend_welcome'
  binding=db.get(WechatSessionBinding,b['binding_id'])
  context=c3_service._build_ai_context(db,binding,c3_service._conversation_for_binding(db,binding),batch)
  assert context['messages']==[] and context['brain_context_snapshot']['prior_messages']==[]
  receipt=db.scalar(select(HookHistoryReceipt));assert receipt.payload_json['coverage']['scans'][0]['databases'][0]['pages'][0][0]['StrContent']==NOTICE
  generated=c3_service.generate_for_batch(db,batch_id=batch.id,native_client_admitted=True);assert generated['decision']=='send_reply'
  action=db.get(ReplyAction,generated['reply_action_id']);assert action.reply_text
  assert batch.ai_response_snapshot['raw_payload']['trigger_type']=='friend_welcome'
  db.commit()
 repeat,_=upload(w,p,b,value,monkeypatch);assert repeat.status_code==200
 assert repeat.json()['data']['message_batch']['batch_id']==data['message_batch']['batch_id']
 with SessionLocal() as db:
  assert db.scalar(select(func.count()).select_from(MessageBatch))==1
  assert db.scalar(select(func.count()).select_from(ReplyAction))==1


def test_real_customer_input_after_notice_is_the_only_ai_input(monkeypatch):
 w,t,p,b=bound();value=with_rows(b,[NOTICE,'预算15万元，先不要报价。'])
 result,_=upload(w,p,b,value,monkeypatch);assert result.status_code==200,result.text
 with SessionLocal() as db:
  data=result.json()['data'];batch=db.get(MessageBatch,data['message_batch']['batch_id'])
  assert batch.trigger_type=='customer_message'
  binding=db.get(WechatSessionBinding,b['binding_id']);context=c3_service._build_ai_context(db,binding,c3_service._conversation_for_binding(db,binding),batch)
  assert [m['content'] for m in context['messages']]==['预算15万元，先不要报价。']
  assert db.scalar(select(func.count()).select_from(MessageBatch))==1


def test_previously_saved_notice_is_retained_but_not_used_as_customer_context(monkeypatch):
 w,t,p,b=bound();value=with_rows(b,[NOTICE]);row=value['scans'][0]['databases'][0]['pages'][0][0]
 from app.services.hook_message_service import message_key
 key=message_key(p['self_wxid'],b['peer_wxid'],row['MsgSvrID'])
 with SessionLocal() as db:
  event=MessageEvent(conversation_id=b['conversation_id'],binding_id=b['binding_id'],worker_id=w['id'],
      rpa_session_key='hook:'+b['binding_id'],read_run_id=str(uuid.uuid4()),
      source_message_key=key,dedupe_key=key,sender_role='customer',message_type='text',content=NOTICE,
      raw_payload={'transport':'hook','native_msg_id':row['MsgSvrID']},item_state='text_ready')
  db.add(event);db.commit();mid=event.id
 result,_=upload(w,p,b,value,monkeypatch);assert result.status_code==200,result.text
 with SessionLocal() as db:
  event=db.get(MessageEvent,mid);assert event.content==NOTICE and event.item_state=='withdrawn'
  assert event.raw_payload['native_system_notice'] and mid not in result.json()['data']['message_event_ids']


@pytest.mark.parametrize('text,direction,excluded',[
 (NOTICE,'0',True),(NOTICE+'。','0',True),(NOTICE.replace('，',','),'0',True),
 ('你好，我们可以开始聊天了','0',False),('我刚通过了你的朋友验证请求，预算15万','0',False),
 ('引用：'+NOTICE,'0',False),(NOTICE+'，想看SUV','0',False),(NOTICE,'1',False),
])
def test_exact_notice_only_never_broadly_filters_customer_speech(text,direction,excluded,monkeypatch):
 w,t,p,b=bound();value=with_rows(b,[text])
 for scan in value['scans']:scan['databases'][0]['pages'][0][0]['IsSender']=direction
 checked=shared_adapter('hook_history_rules').validate_coverage(value,b['peer_wxid'],int(b['history_since_create_time']))
 assert bool(checked['system_notice_native_ids'])==excluded
 assert len(checked['semantic_rows'])==(0 if excluded else 1)
