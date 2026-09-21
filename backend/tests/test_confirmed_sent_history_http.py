"""Real Worker receipts, HTTP, SQLite and server DB; model/desktop controlled.

The seed pass really generates, claims and acknowledges its reply. Only later
captured OCR of that same sent bubble loses a character. No test inserts an
AI receipt or calls generation to rescue the continuation.
"""
import json

import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.wechat import MessageEvent
import test_historical_confidence_worker_http as chain
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation


@pytest.mark.parametrize('mode,segmented,media,changes', [
    ('normal', False, None, False), ('normal', True, None, False),
    ('hc_typing', False, None, False), ('hc_typing', True, None, False),
    ('normal', False, 'voice', False), ('normal', False, 'image', False),
    ('hc_loss', False, None, False), ('suppress_async', False, None, False),
    ('normal', False, None, True), ('normal', False, 'voice', True),
    ('normal', False, 'image', True),
    ('normal', False, 'voice', 'prepare'),
    ('normal', False, 'image', 'alternating'),
    ('normal', False, 'voice', 'expanded'),
])
def test_confirmed_sent_history_continues_through_original_pipeline(
        http_api, monkeypatch, async_generation, tmp_path, mode, segmented, media, changes):
    original = chain.worker_source

    def source():
        value = original()
        before = "payload=self._contractual_message_payload({'messages':copy.deepcopy(self.messages),'tail_complete':True})"
        after = """captured=copy.deepcopy(self.messages)
  if os.environ['HC_RUN']=='continued':
   for message in captured:
    if message['id']=='sent-4':
     assert message['content']=='好的，我帮您查询看车安排。'
     message['content']='好的，我您查询看车安排。'
  payload=self._contractual_message_payload({'messages':captured,'tail_complete':True})"""
        if media:
            before = "payload={'messages':copy.deepcopy(self.messages),'tail_complete':True}"
            after = after.replace("payload=self._contractual_message_payload({'messages':captured,'tail_complete':True})",
                                  "payload={'messages':captured,'tail_complete':True}")
        if changes:
            predicate=("True" if changes=='expanded' else "getattr(self,'capture_count',0)%2==0" if changes=='alternating'
                       else "not getattr(self,'receipt_noisy_capture_used',False)")
            after=after.replace("os.environ['HC_RUN']=='continued'", "os.environ['HC_RUN']=='continued' and "+predicate)
            after=after.replace("message['content']='好的，我您查询看车安排。'",
                                "message['content']='好的，我您查询看车安排。'\n     self.receipt_noisy_capture_used=True")
        assert before in value
        value=value.replace(before, after, 1)
        if changes=='expanded':
            value=value.replace("message['content']='好的，我您查询看车安排。'",
                "message['content']=('好的，我帮您查询看车安排。' if getattr(self,'capture_count',0)==0 else '无法还原的破损文本' if getattr(self,'capture_count',0)==1 else '好的，我您查询看车安排。')")
            value=value.replace('  self.get_messages_payloads=[payload]',
                "  if kwargs.get('history_mode')=='anchor_until_found':\n   payload['history_load']={'ok':True,'anchor_found':True,'restored_to_latest':True}\n   Path(__file__).with_name('expansion-captured.json').write_text(json.dumps(payload,ensure_ascii=False))\n  self.get_messages_payloads=[payload]",1)
        if changes=='prepare':
            value=value.replace(' def prepare_voice_action(self,**kwargs):',
                " def prepare_voice_action(self,**kwargs):\n  self.get_messages(display_name='CJTEST01',rpa_session_key='')")
        value=value.replace(' response=original(request,**kwargs)', '''
 if request.url.endswith('/messages/ingest') and os.environ['HC_RUN']=='continued':
  body=json.loads(request.body)
  proof=(body['evidence'].get('sequence_alignment_evidence') or {}).get('text_correspondence') or {}
  if proof.get('confirmed_sent_receipts') and not globals().get('receipt_negatives_done'):
   globals()['receipt_negatives_done']=True
   Path(__file__).with_name('confirmed-receipt-request.json').write_text(json.dumps(body,ensure_ascii=False))
   negatives=[]
   for damage in ('action','time','body','stable_id','omit_reply','role'):
    bad=copy.deepcopy(body)
    p=bad['evidence']['sequence_alignment_evidence']['text_correspondence']
    receipt=p['confirmed_sent_receipts'][0]
    item=next(m for m in bad['messages'] if m['sender_role_hint']=='self')
    if damage=='action':receipt['reply_action_id']='00000000-0000-0000-0000-000000000000'
    if damage=='time':receipt['confirmed_at']='2000-01-01T00:00:00+00:00'
    if damage=='body':receipt['reply_text']='完全不同的另一条回复'
    if damage=='stable_id':receipt['worker_stable_id']='worker-message-999'
    if damage=='omit_reply':bad['messages'].remove(item)
    if damage=='role':item['sender_role_hint']='customer'
    changed=request.copy();changed.prepare_body(data=None,files=None,json=bad)
    denied=original(changed,**kwargs)
    negatives.append({'damage':damage,'status':denied.status_code,'response':denied.json()})
    assert denied.status_code in {400,409,422},negatives
   Path(__file__).with_name('confirmed-receipt-negatives.json').write_text(json.dumps(negatives))
 response=original(request,**kwargs)''',1)
        return value

    monkeypatch.setattr(chain, 'worker_source', source)
    chain.test_confidence_read_reaches_async_send_ack_and_flow(
        http_api, monkeypatch, async_generation, tmp_path, mode, segmented, media)
    if mode not in {'suppress_async'}:
        result = json.loads((tmp_path/'result.json').read_text())
        assert not result['pending_c2_outbox']
    evidence=json.loads((tmp_path/'confirmed-receipt-request.json').read_text())
    proof=evidence['evidence']['sequence_alignment_evidence']['text_correspondence']
    if changes=='expanded':assert (tmp_path/'expansion-captured.json').is_file()
    assert proof['confirmed_sent_receipts']
    with SessionLocal() as db:
        events=list(db.scalars(select(MessageEvent)))
        event=next((e for e in events if e.raw_payload.get('ai_reply_action_id')==proof['confirmed_sent_receipts'][0]['reply_action_id']),None)
        assert event is not None and event.raw_payload['sender_source']=='ai'
        assert event.raw_payload['ai_reply_action_id']==proof['confirmed_sent_receipts'][0]['reply_action_id']
        assert event.content in {'好的，我您查询看车安排。','好的，我帮您查询看车安排。'}
