"""Original incident pixels + real Worker/SQLite/socket HTTP/PG/background task.

The visible frame boundary replays recorded observations, then the corrected
reading. Original-image OCR is a real Sidecar subprocess. Desktop sends and
the external model are controlled; no test submits recovery receipts or starts
replacement generation. This is not Windows physical-send acceptance.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select
import test_c3_api as fixtures
from test_lead_followup_eligibility import http_api, isolated_db
from test_pre_send_checkpoint_order import async_generation
from test_historical_text_correction import original_png_proposal
from test_reply_sequence_worker import WORKER
from test_reply_sequence_http import PARTS
from app.core.database import SessionLocal
from app.models.c3 import Conversation, HandoffEvent, ReplyAction, SentAck
from app.models.wechat import MessageEvent
from app.models.worker import Worker
from app.models.message_text_correction import MessageTextCorrection
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision


FRAME_IO = r'''
 def get_messages(self,**kwargs):
  if not hasattr(self,'original_rows'):
   self.original_rows=json.loads(Path(os.environ['H_SOURCE']).read_text())['evidence']['observations']
   from PIL import Image
   import hashlib
   from apps.wechat_ai_customer_service.adapters.historical_text_correction import captured_png_evidence
   image=Image.open(os.environ['H_ORIGINAL_IMAGE']).convert('RGB')
   self.png_evidence=captured_png_evidence(os.environ['H_ORIGINAL_IMAGE'],raw_rgb_sha256=hashlib.sha256(image.tobytes()).hexdigest())
  rows=copy.deepcopy(self.original_rows)
  # The saved evidence includes identities assigned AFTER the original
  # Sidecar returned. The physical boundary must not impersonate the Worker.
  for row in rows: row['source_message'].pop('source_message_key',None)
  if mode=='resume' or (self.sent_replies if os.environ.get('H_SEGMENTED')=='1' else self.message_reads):
   row=next(o for o in rows if o['observation_id']=='win32_ocr:7c547101cc7aa38e')
   row['content_clean']='二手车'
  for message in self.messages:
   if message['id'].startswith('sent-'):
    extra=self._contractual_message_payload({'messages':[message]})['observations'][0]
    top=640+50*(len(rows)-len(self.original_rows))
    extra['bubble_rect']=[400,top,720,top+38]
    rows.append(extra)
  self.get_messages_payloads=[{'ok':True,'observations':rows,'tail_complete':True,
   'screenshot_path':os.environ['H_ORIGINAL_IMAGE'],'sidecar_run_id':'incident-replay-original',
   'frame_observation':{'frame_id':'incident-replay-original','screenshot_path':os.environ['H_ORIGINAL_IMAGE'],'png_byte_evidence':self.png_evidence},
   'send_context_guard':{**self._send_context_guard(rows),'message_viewport_bounds':[302,78,768,698]}}]
  return FakeBridge.get_messages(self,**kwargs)
 def recheck_original_message(self,**kwargs):
  from chejin_worker_client.rpa_bridge import RpaBridge
  import subprocess
  proxy=RpaBridge()
  def offline(args,**kw):
   counter=Path(__file__).with_name('offline-ocr-count.json')
   counter.write_text(str((int(counter.read_text()) if counter.exists() else 0)+1))
   root=Path(os.environ['CHEJIN_FIX_ROOT'])/'worker-client/omniauto-rpa'
   env={**os.environ,'PYTHONPATH':str(root)}
   program=root/'apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py'
   result=subprocess.run([os.environ['CHEJIN_REAL_OCR_PYTHON'],str(program),*args],
     env=env,capture_output=True,text=True,timeout=60)
   Path(__file__).with_name('offline-ocr.stdout').write_text(result.stdout)
   Path(__file__).with_name('offline-ocr.stderr').write_text(result.stderr)
   assert result.returncode==0,result.stderr
   return json.loads(result.stdout.strip().splitlines()[-1])
  proxy._call_omniauto=offline
  return proxy.recheck_original_message(**kwargs)
'''

RECOVER_AND_START = r'''
 Path(__file__).with_name('initial-result.json').write_text(json.dumps({'result':result,'wire':exchanges,'errors':errors},ensure_ascii=False,default=str))
 assert binding.run_status=='faulted',{'status':binding.run_status,'errors':errors,'result':result}
 from chejin_worker_client import storage
 prefix=list(bridge.sent_replies)
 assert len(prefix)==(int(os.environ.get('H_SEGMENTED','0')) if mode!='resume' else 0)
 runner.start(binding)
 deadline=time.monotonic()+20
 while time.monotonic()<deadline:
  if runner.fault_recovery_state().get('ready'):break
  time.sleep(.05)
 Path(__file__).with_name('before-start.json').write_text(json.dumps({
  'ready':runner.fault_recovery_state(),'runtime':load_runtime_control(),'wire':exchanges,'errors':errors,
  'safety':runner.update_install_safety_snapshot(),
  'outbox':storage.list_c2_outbox_waiting()},ensure_ascii=False,default=str))
 assert runner.fault_recovery_state().get('ready'),(runner.fault_recovery_state(),errors)
 assert bridge.sent_replies==prefix
 assert runner.set_run_status('running')
 deadline=time.monotonic()+20
 while time.monotonic()<deadline:
  if len(bridge.sent_replies)==len(prefix)+1 and not load_runtime_control()['inflight_flow_id']:break
  time.sleep(.05)
 runner.stop_event.set()
 for thread in (runner.thread,runner.c2_thread,runner.thread_monitor):
  if thread:thread.join(5)
'''


def historical_worker_script():
    start = WORKER.index(' def get_messages(self,**kwargs):')
    end = WORKER.index(' def send_reply(self,**kwargs):', start)
    value = WORKER[:start] + FRAME_IO + WORKER[end:]
    value = value.replace("on_result=lambda _:None,on_error=errors.append)",
        "on_result=lambda _:None,on_error=errors.append,can_pull_tasks=lambda:False)")
    old_start=value.index(" if mode=='resume':\n  runner.start(binding)",value.index('runner=TaskRunner'))
    old_end=value.index('\n else:\n  target=',old_start)
    value=value[:old_start]+" if mode=='resume':\n  result={'resuming_saved_sqlite':True}"+value[old_end:]
    recovery = r'''
 Path(__file__).with_name('visible.json').write_text(json.dumps(bridge.messages))
 if os.environ.get('H_LOSS') and mode!='resume':
  runner.start(binding)
  deadline=time.monotonic()+20
  while time.monotonic()<deadline:
   if Path(__file__).with_name('network-loss.json').exists():break
   time.sleep(.05)
  assert Path(__file__).with_name('network-loss.json').exists()
  runner.stop_event.set()
  for thread in (runner.thread,runner.c2_thread,runner.thread_monitor):
   if thread:thread.join(5)
  assert has_pending_c2_outbox()
 else:
''' + '\n'.join(' ' + line if line else '' for line in RECOVER_AND_START.splitlines()) + '\n'
    value = value.replace(" out={'result':result,'sent':", recovery + " out={'result':result,'sent':")
    value = value.replace("errors=[]\nrunner=", "from chejin_worker_client import omniauto_vision\nomniauto_vision.vision_configuration_status=lambda:{'ready':True}\nerrors=[]\nrunner=")
    value = value.replace('bridge=Wechat()', 'bridge=Wechat()\nbridge.sidecar_active=lambda:False')
    value = value.replace('runner.binding=binding', """runner.binding=binding
if os.environ.get('AUDIT_DISABLE_H_PREPARE')=='1':
 from chejin_worker_client import historical_correction_recovery
 historical_correction_recovery.prepare=lambda *args,**kwargs:False
""")
    value = value.replace(
        '  result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=True)',
        '''  if os.environ.get('H_ORDINARY_READ')=='1':
   first=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=False)
   batch=first['result']['message_batch']
   deadline=time.monotonic()+10
   while time.monotonic()<deadline:
    status=api.get_wechat_message_batch(binding,batch['batch_id'])
    if status.get('reply_action'):break
    time.sleep(.05)
   assert status.get('reply_action'),status
   target=next(t for t in api.get_wechat_read_targets(binding) if t.conversation_id==conversation_id)
   result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=False)
  else:
   result=runner._read_one_wechat_target(binding,target,enforce_read_targets=True,wait_for_brain=True)''')
    value = value.replace("  frame=self._contractual_message_payload({'messages':copy.deepcopy(self.messages)})",
        "  frame=self.get_messages(display_name='CJTEST01',rpa_session_key='')")
    value = value.replace(' response=original(request,**kwargs)', r'''
 loss=os.environ.get('H_LOSS','') if mode!='resume' else ''
 affected=(loss.startswith('correction_') and request.url.endswith('/message-text-corrections')) or (loss=='receipt_response' and request.url.endswith('/fail'))
 def lost():
  Path(__file__).with_name('network-loss.json').write_text(json.dumps({'path':request.url,'mode':loss}))
  raise requests.ConnectionError('controlled network loss')
 if affected and loss.endswith('_request'):lost()
 response=original(request,**kwargs)
 if affected:lost()
''')
    return value.replace('C3TEST01', 'CJTEST01')


@pytest.mark.parametrize('segmented,transport',[(False,'normal'),(True,'normal'),(False,'correction_request'),(False,'correction_response'),(True,'receipt_response'),(False,'ordinary_read')])
def test_original_pixel_correction_then_explicit_start_replies_once(original_png_proposal, http_api, monkeypatch, async_generation, tmp_path, segmented, transport):
    monkeypatch.setattr(fixtures, 'client', http_api)
    if transport == 'ordinary_read':
        from app.services import wechat_service
        # Compress the two-minute ordinary polling cooldown only. Admission,
        # fresh checkpoint, Flow ownership and receipts remain production code.
        monkeypatch.setattr(wechat_service, 'READ_SUCCESS_COOLDOWN_SECONDS', 0)
    class Model:
        requests = []
        def generate_reply_decision(self, **kwargs):
            type(self).requests.append(kwargs)
            if segmented and len(type(self).requests)==1:
                return AIEngineDecision(decision='send_reply',reply_text=' '.join(PARTS),guard_result='pass',
                    raw_payload={'omniauto_brain_result':{'brain_plan':{'reply_segments':PARTS}}})
            return AIEngineDecision(decision='send_reply', reply_text='好的，我来介绍一下看车安排。', guard_result='pass')
    monkeypatch.setattr(c3_service, 'get_ai_engine_adapter', Model)
    if os.environ.get('AUDIT_DISABLE_H_CALLBACK')=='1':
        from app.api.routes import wechat
        native=wechat._generate_message_batch
        def initial_only(*args,**kwargs):
            if not Model.requests:return native(*args,**kwargs)
        monkeypatch.setattr(wechat,'_generate_message_batch',initial_only)
    worker = fixtures._create_worker()
    fixtures._create_sales(worker['id'])
    fixtures._create_lead(remark_code='CJTEST01')
    target = fixtures._scan(worker, remark_code='CJTEST01')
    with SessionLocal() as db:
        conversation = db.get(Conversation, target['conversation_id'])
        conversation.status, conversation.friend_state = 'waiting_user_reply', 'friend_active'
        db.get(Worker, worker['id']).local_lock_summary = {'capabilities': {
            'pre_send_read_recovery_version': 1, 'historical_text_correction_version': 1,
            'reply_sequence_version': 1, 'text_correspondence_version': 1}}
        db.commit()
    source, proposal = original_png_proposal
    source_path = tmp_path/'source.json'; source_path.write_text(json.dumps(source,ensure_ascii=False))
    image_path = tmp_path/'worker/artifacts/incident-replay-original/original.png'
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(base64.b64decode(proposal['image_base64']))
    script = tmp_path/'worker.py'; script.write_text(historical_worker_script())
    env = {**os.environ,'CHEJIN_WORKER_HOME':str(tmp_path/'worker'),'CHEJIN_RPA_MODE':'mock',
           'CHEJIN_C2_ENABLED':'true','H_SOURCE':str(source_path),'H_ORIGINAL_IMAGE':str(image_path),
           'H_SEGMENTED':'1' if segmented else '0'}
    if transport == 'ordinary_read': env['H_ORDINARY_READ']='1'
    elif transport!='normal':env['H_LOSS']=transport
    def execute(mode):
        result = subprocess.run([sys.executable,str(script),http_api.get('/healthz').url.removesuffix('/healthz'),
            json.dumps(worker),target['conversation_id'],mode], env=env,capture_output=True,text=True,timeout=100)
        (tmp_path/(mode+'.stdout')).write_text(result.stdout); (tmp_path/(mode+'.stderr')).write_text(result.stderr)
        assert result.returncode==0,result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])
    first=execute('normal')
    records=[first]
    if transport not in {'normal','ordinary_read'}:
        assert first['pending_c2_outbox']
        records.append(execute('resume'))
    record=records[-1]
    (tmp_path/'result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
    (tmp_path/'model-requests.json').write_text(json.dumps(Model.requests,ensure_ascii=False,indent=2,default=str))
    assert [text for r in records for text in r['sent']]==(PARTS[:1] if segmented else [])+['好的，我来介绍一下看车安排。'],records
    assert int((tmp_path/'offline-ocr-count.json').read_text())==1
    assert not record['pending_c2_outbox'] and not record['pending_ack'] and not record['runtime']['inflight_flow_id']
    assert len(Model.requests)==2 and async_generation['counts']['generated']==2
    with SessionLocal() as db:
        corrections=list(db.scalars(select(MessageTextCorrection)))
        assert len(corrections)==1
        assert db.get(MessageEvent,corrections[0].message_event_id).content=='手车'
        assert len(list(db.scalars(select(SentAck))))==(2 if segmented else 1)
        assert not list(db.scalars(select(HandoffEvent)))
        assert sorted(a.status for a in db.scalars(select(ReplyAction)))==(['cancelled','failed','sent','sent'] if segmented
            else ['cancelled','sent'] if transport=='ordinary_read' else ['failed','sent'])
    if segmented:
        prefix=Model.requests[-1]['conversation_context']['brain_context_snapshot']['partial_reply_recovery']['confirmed_prefix']
        assert [p['text'] for p in prefix]==PARTS[:1]
