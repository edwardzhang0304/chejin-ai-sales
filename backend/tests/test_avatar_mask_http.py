"""Private original PNG → real OCR → Worker/SQLite → socket HTTP/PostgreSQL.

External model and desktop transport are controlled. No test submits an ACK,
finishes a Flow or invokes generation; only production Worker entrypoints do.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import timedelta

import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.c3 import Conversation, ReplyAction, SentAck, HandoffEvent
from app.models.task import Task
from app.models.wechat import WechatSessionBinding, MessageEvent
from app.models.worker import Worker
from app.core.config import get_settings
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision
from test_lead_followup_eligibility import isolated_db, http_api, fixture_rows
from test_avatar_text_input import real_frames
from test_c2_identity_gate_settlement import SCRIPT, ROOT


def test_original_avatar_frame_automatically_continues_after_confirmed_reply(
    http_api, tmp_path, monkeypatch, real_frames,
):
    before = real_frames['before'][0]
    after = real_frames['after'][0]
    assert len(before['observations']) == 5
    assert len(after['observations']) == 6
    text = after['observations'][-2]['content_clean']
    class Model:
        requests = []
        def generate_reply_decision(self, **kwargs):
            self.requests.append(kwargs)
            return AIEngineDecision(decision='send_reply', reply_text=text if len(self.requests)==1 else '好的，我按日常代步需求继续帮您筛选。', guard_result='pass')
    monkeypatch.setattr(get_settings(), 'c3_ai_adapter_mode', 'real')
    monkeypatch.setattr(c3_service, 'get_ai_engine_adapter', Model)
    suppressed = []
    if os.environ.get('AVATAR_MASK_DISABLE_AUTO_CALLBACK') == '1':
        from app.api.routes import wechat
        native = wechat._generate_message_batch
        def dispatch(*a, **kw):
            if Model.requests:
                suppressed.append(True)
                return
            return native(*a, **kw)
        monkeypatch.setattr(wechat, '_generate_message_batch', dispatch)
    w, rows = fixture_rows(eligible=True)
    row = rows[0]
    with SessionLocal() as db:
        binding = db.get(WechatSessionBinding, row['binding_id'])
        binding.remark_code = before['target_confirmation']['confirmed_target']
        conversation = db.get(Conversation, row['conversation_id'])
        conversation.friend_state = 'friend_active'
        db.commit()
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import sanitize_sidecar_contract_output
    (tmp_path/'frames.json').write_text(json.dumps(sanitize_sidecar_contract_output(
        {'seed':before, 'current':after, 'post_send':after,
         'target':before['target_confirmation']['confirmed_target']}), ensure_ascii=False))
    # Only the simulated physical send result changes: the production
    # confirmation comparator receives both original ordered frame sequences.
    old = "bridge.send_payload={**bridge.send_payload, **TaskRunnerTest._confirmed_send_sidecar_result(observations=observed, confirmed_observation_id=observed[-2]['observation_id'], run_id='controlled-historical-send')}"
    new = """from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
 def snapshot(frame):
  return {'ok':True,'validation':{'confirmed_target':inputs['target']},'input_region':{'has_visible_text':False},'frame_observation':frame['frame_observation'],'observations':frame['observations'],
   'message_sequence':[{'observation_id':o['observation_id'],'sender_role':o['sender_role'],'row_kind':o['row_kind'],'content_normalized':o.get('content_clean','')} for o in frame['observations']]}
 baseline=snapshot(inputs['seed']);current=snapshot(inputs['post_send'])
 confirmed=sidecar.confirm_reply_sent(1,target=inputs['target'],text=observed[-2]['content_clean'],exact=True,baseline_match_count=0,baseline_message_sequence=baseline['message_sequence'],initial_snapshot=current,max_attempts=1)
 assert confirmed['ok'],confirmed
 bridge.send_payload={**bridge.send_payload,'sidecar_run_id':'controlled-original-frame-send','send_result':{'ok':True,'confirmed':True,'result':'sent','send_baseline':baseline,'sent_confirmation':confirmed}}"""
    assert SCRIPT.count(old)==1
    script=SCRIPT.replace(old,new)
    # Correct wait mode is limited to first reply. Second original-frame read
    # must settle itself and leave its automatically generated reply pending.
    path=tmp_path/'worker.py';path.write_text(script)
    env={**os.environ,'CHEJIN_WORKER_HOME':str(tmp_path/'worker'), 'CHEJIN_RPA_MODE':'mock',
         'PYTHONPATH':os.pathsep.join([os.environ.get('PYTHONPATH',''),str(ROOT/'worker-client/tests')])}
    base=http_api.get('/healthz').url.removesuffix('/healthz')
    def run(phase):
        p=subprocess.run([sys.executable,str(path),base,json.dumps(w),json.dumps(row),'historical_frame',phase],env=env,text=True,capture_output=True,timeout=80)
        (tmp_path/(phase+'.stdout')).write_text(p.stdout)
        (tmp_path/(phase+'.stderr')).write_text(p.stderr)
        assert p.returncode==0,p.stdout+p.stderr
        return json.loads(p.stdout.splitlines()[-1])
    first=run('seed')
    assert first['result']['ok'],first
    assert first['physical_sends']==1,first
    assert len(first['identity_state']['ai_reply_receipts'])==1,first
    assert not first['runtime']['inflight_flow_id'] and not first['pending'] and not first['locked'],first
    with SessionLocal() as db:
        b=db.get(WechatSessionBinding,row['binding_id'])
        b.unread_generation+=1;b.unread_hint=True;b.next_read_due_at=utcnow()-timedelta(seconds=1)
        db.commit()
    second=run('read')
    assert second['result']['ok'],second
    assert second['physical_sends']==0 and not second['pending'] and not second['locked'],second
    assert not second['runtime']['inflight_flow_id'],second
    until=time.monotonic()+5
    while True:
        with SessionLocal() as db:
            actions=list(db.scalars(select(ReplyAction).where(ReplyAction.conversation_id==row['conversation_id'])))
            tasks=list(db.scalars(select(Task).where(Task.reply_action_id.in_([a.id for a in actions]))))
        if len(actions)==2 or time.monotonic()>until:break
        time.sleep(.05)
    evidence={'before':first,'after':second,'automatic_actions':[a.id for a in actions],
              'automatic_tasks':[t.id for t in tasks],'model_calls':len(Model.requests),'suppressed_callbacks':len(suppressed)}
    (tmp_path/'automatic-result.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str))
    assert len(actions)==len(tasks)==len(Model.requests)==2, {'automatic_reply_missing':evidence}
    with SessionLocal() as db:
        assert len(list(db.scalars(select(SentAck))))==1
        assert not list(db.scalars(select(HandoffEvent)))
        assert not db.get(Worker,w['id']).inflight_flow_state
        events=list(db.scalars(select(MessageEvent).where(MessageEvent.conversation_id==row['conversation_id'])))
        assert len(events)==7,[(e.sender_role,e.content) for e in events]
        assert all('UNI' not in (e.content or '') for e in events)
