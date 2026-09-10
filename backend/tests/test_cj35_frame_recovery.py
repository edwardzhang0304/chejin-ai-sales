"""Private real OCR replay -> production Worker child -> HTTP/PostgreSQL.

External AI and WeChat sends are controlled. No manual generation or Flow
settlement. The historical AI send uses the real current-frame observation as
its external confirmation; this is not the customer's original SQLite or EXE.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import timedelta

import pytest
from sqlalchemy import select,func
from test_lead_followup_eligibility import isolated_db,http_api,fixture_rows
from test_c2_identity_gate_settlement import SCRIPT
from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.wechat import WechatSessionBinding,MessageEvent
from app.models.worker import Worker
from app.models.c3 import Conversation,HandoffEvent,MessageBatch,ReplyAction
from app.models.sales import Sales
from app.models.lead import Lead
from app.models.task import Task

ROOT=Path(__file__).resolve().parents[2]

@pytest.mark.parametrize('mode',['original','partial_top','partial_ocr_mismatch','technical_frame','dense_media'])
def test_original_history_to_autonomous_reply_or_technical_finish(http_api,tmp_path,monkeypatch,mode):
    source=os.environ.get('CHEJIN_CJ35_FIXED_REPLAY')
    assert source,'Set CHEJIN_CJ35_FIXED_REPLAY to the real production OCR replay evidence'
    frames=json.loads(Path(source).read_text())
    for f in frames:
        assert hashlib.sha256(Path(f['file']).read_bytes()).hexdigest()==f['sha256']
    previous=frames[0]['result'];current=frames[1]['result']
    if mode=='dense_media':
        from test_image_prefix_boundary import public_prefix_frame
        current=public_prefix_frame(tmp_path,monkeypatch,dense=True)
        assert not current['ok'] and current['error_code']=='C2_IMAGE_OBSERVATION_FAILED',current
        assert current['avatar_evidence']['reason']=='image_candidate_spans_multiple_avatar_rows',current
    if mode in {'partial_top','partial_ocr_mismatch'}:
        from PIL import Image
        from unittest.mock import patch
        from test_chat_viewport_boundary import calibrated_frame,s
        original=Image.open(frames[1]['file']).convert('RGB')
        image=original.copy();left,top,right,bottom=frames[1]['layout']['message_viewport_bounds']
        # Labelled derivative: scroll only the chat content upward by 24 px.
        image.paste(original.crop((left,top+24,right,bottom)),(left,top))
        image.paste((250,250,250),(left,bottom-24,right,bottom))
        # A real scroll does not move the fixed right window border. Preserve
        # its measured, constant-colour columns in this synthetic derivative.
        border_left=right
        border_colour=original.getpixel((right-1,top))
        while border_left>left and all(original.getpixel((border_left-1,y))==border_colour for y in range(top,bottom)):
            border_left-=1
        image.paste(original.crop((border_left,top,right,bottom)),(border_left,top))
        if mode=='partial_top':
            from PIL import ImageDraw
            image=original.copy();draw=ImageDraw.Draw(image)
            # A separate, labelled overlay fixture: taller header clips the
            # old top message while lower pixels remain at their positions.
            draw.rectangle((left,top-2,border_left-1,top+23),fill=(250,250,250))
            draw.line((left,top+23,border_left-1,top+23),fill=(240,240,240))
        path=tmp_path/'synthetic-scroll.png';image.save(path)
        _,geometry=calibrated_frame(image)
        with patch.object(s,'capture_wechat',return_value=(image,str(path))),patch.object(s,'get_window_geometry',return_value=geometry),patch.object(s,'window_dpi_scale',return_value=1):
            try:
                current=s.sanitize_sidecar_contract_output(s.messages_payload(1,{},target='CJ35C76M',history_load_times=0,confirm_target='CJ35C76M',confirm_exact=True,artifact_dir=str(tmp_path/'partial-report')))
            except Exception as exc:
                cause=exc
                while cause is not None:
                    if isinstance(cause,s.frame_avatars.AvatarEvidenceError):
                        (tmp_path/'partial-error.json').write_text(json.dumps(cause.evidence,ensure_ascii=False,indent=2))
                    cause=cause.__cause__
                raise
        assert current['ok'] and current['top_message_fragment'],current
    from app.core.config import get_settings
    from app.services import c3_service
    from app.services.ai_adapter import MockOmniAutoAIEngineAdapter,AIEngineDecision
    monkeypatch.setattr(get_settings(),'c3_ai_adapter_mode','real')
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',MockOmniAutoAIEngineAdapter)
    provider_calls=[]
    def decision(self,**kw):
        provider_calls.append(1)
        return AIEngineDecision(decision='send_reply',reply_text=frames[1]['result']['messages'][-2]['content'].replace('\n',''),confidence=.9,guard_result='pass',evidence_refs=[],risk_flags=[],raw_payload={'adapter':'controlled external provider'})
    monkeypatch.setattr(MockOmniAutoAIEngineAdapter,'generate_reply_decision',decision)
    w,rows=fixture_rows();row=rows[0]
    with SessionLocal() as db:
        b=db.get(WechatSessionBinding,row['binding_id']);b.remark_code=b.display_name='CJ35C76M'
        for h in db.scalars(select(HandoffEvent).where(HandoffEvent.conversation_id==row['conversation_id'])):h.deleted_at=utcnow()
        c=db.get(Conversation,row['conversation_id']);c.status='waiting_user_reply';c.friend_state='friend_active'
        sales=Sales(sales_name='Synthetic',phone='13800009992',worker_id=w['id'],enabled=True);db.add(sales);db.flush()
        db.get(Lead,row['lead_id']).sales_id=sales.id;b.sales_id=sales.id;db.commit()
    (tmp_path/'frames.json').write_text(json.dumps({'seed':previous,'current':current,'post_send':frames[1]['result']},ensure_ascii=False))
    script=SCRIPT
    if mode=='technical_frame':
        script=script.replace("bridge.get_messages_payloads=[inputs['seed' if phase=='seed' else 'current']]", "bridge.get_messages_payloads=[inputs['seed' if phase=='seed' else 'current']]\nif phase=='read': bridge.locate_payloads=[{'ok':False,'error_code':'C2_AVATAR_EVIDENCE_INVALID','reason':'controlled real Sidecar error boundary'}]")
    script_path=tmp_path/'worker.py';script_path.write_text(script)
    env={**os.environ,'CHEJIN_WORKER_HOME':str(tmp_path/'worker'),'CHEJIN_RPA_MODE':'mock','CHEJIN_UI_LOCK_LEASE_SECONDS':'1',
        'PYTHONPATH':os.pathsep.join([str(ROOT/'worker-client'),str(ROOT/'worker-client/tests'),str(ROOT/'worker-client/omniauto-rpa'),os.environ.get('PYTHONPATH','')])}
    base=http_api.get('/healthz').url.removesuffix('/healthz')
    def run(phase):
        p=subprocess.run([sys.executable,str(script_path),base,json.dumps(w),json.dumps(row),'historical_frame',phase],env=env,capture_output=True,text=True,timeout=50)
        (tmp_path/(phase+'.stdout')).write_text(p.stdout);(tmp_path/(phase+'.stderr')).write_text(p.stderr)
        assert p.returncode==0,p.stderr
        return json.loads(p.stdout.splitlines()[-1])
    seed=run('seed');assert seed['result']['ok'],seed
    assert seed['physical_sends']==1 and not seed['runtime']['inflight_flow_id'],seed
    assert len(seed['identity_state']['ai_reply_receipts'])==1,seed
    with SessionLocal() as db:
        b=db.get(WechatSessionBinding,row['binding_id']);b.unread_generation=1;b.unread_hint=True;b.next_read_due_at=utcnow()-timedelta(seconds=1);db.commit()
        before_messages=db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.conversation_id==row['conversation_id']))
        before_customers=db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.conversation_id==row['conversation_id'],MessageEvent.sender_role=='customer'))
        before_tasks=db.scalar(select(func.count()).select_from(Task).where(Task.task_type=='chat_reply'))
    before_provider_calls=len(provider_calls)
    if os.environ.get('CHEJIN_CJ35_DISABLE_AUTOMATIC')=='1':
        from app.api.routes import wechat as routes
        monkeypatch.setattr(routes,'_generate_message_batch',lambda *a,**k:None)
    result=run('read')
    assert not result['runtime']['inflight_flow_id'] and not result['pending'] and not result['locked'],result
    deadline=time.monotonic()+5
    while True:
        with SessionLocal() as db:
            counts={'messages':db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.conversation_id==row['conversation_id'])),
                    'customers':db.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.conversation_id==row['conversation_id'],MessageEvent.sender_role=='customer')),
                    'tasks':db.scalar(select(func.count()).select_from(Task).where(Task.task_type=='chat_reply')),
                    'worker_state':db.get(Worker,w['id']).run_status,'flow':db.get(Worker,w['id']).inflight_flow_state}
        if mode in {'technical_frame','partial_ocr_mismatch','dense_media'} or counts['tasks']>before_tasks or time.monotonic()>deadline:break
        time.sleep(.05)
    evidence={'mode':mode,'source_replay_sha256':hashlib.sha256(Path(source).read_bytes()).hexdigest(),'seed':seed,'read':result,
        'counts':counts,'before_messages':before_messages,'before_tasks':before_tasks,'provider_calls_before':before_provider_calls,'provider_calls_after':len(provider_calls),'external_boundaries':'controlled AI and physical WeChat; real Worker, HTTP, PostgreSQL, SQLite'}
    out=Path(os.environ.get('CHEJIN_CJ35_HTTP_EVIDENCE',str(tmp_path)));out.mkdir(parents=True,exist_ok=True)
    (out/(mode+'.json')).write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str))
    if mode in {'technical_frame','partial_ocr_mismatch','dense_media'}:
        assert result['result']['worker_faulted'],result
        assert counts['worker_state']=='faulted' and counts['messages']==before_messages and counts['tasks']==before_tasks,counts
    else:
        assert result['result']['ok'],result
        # Ingest also registers the already-confirmed historical AI receipt.
        assert counts['messages']==before_messages+2,counts
        assert counts['customers']==before_customers+1,counts
        assert counts['tasks']==before_tasks+1,{'automatic_reply_missing':counts}
        assert len(provider_calls)==before_provider_calls+1,provider_calls
        alignment=result['result']['payload']['evidence']['sequence_alignment_evidence']
        assert alignment['new_suffix_observation_ids']==[current['observations'][-1]['observation_id']],alignment
    assert result['physical_sends']==0,result
    assert not counts['flow'] and not result['runtime']['inflight_flow_id'],evidence
    assert not result['locked'] and not result['pending'] and not result['errors'],evidence
    finishes=[x for x in result['exchanges'] if x.get('phase')=='read' and x['url'].endswith('/inflight-flow/finish')]
    assert len(finishes)==1 and finishes[0]['status']==200,finishes
