"""Constructed HC evidence; real HTTP, PG/SQLite, original async generation.

Ordinary historical text uses the negotiated r6 text metric. Original PNG
replay is tested separately; this suite covers authority and tampered proofs.
"""
from copy import deepcopy
import hashlib
import time

import pytest
from sqlalchemy import select
import test_c3_api as api
from test_pre_send_checkpoint_order import FrameInputHTTP, async_generation
from test_lead_followup_eligibility import http_api, isolated_db
from app.core.database import SessionLocal
from app.models.wechat import MessageEvent
from app.models.worker import Worker
from app.models.c3 import Conversation, HandoffEvent, ReplyAction
from app.models.task import Task
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision
from app.services.wechat_service import _identity_checkpoint
from app.contracts.shared_rules import shared_adapter


class HCTransport:
    def __init__(self, http):
        self.http,self.enabled,self.tamper,self.last = http,False,None,None
        self.request = None
        self.seed_messages = {}
    def get(self,*a,**k):return self.http.get(*a,**k)
    def post(self,path,**kwargs):
        if path.endswith('/wechat/messages/ingest'):
            value = deepcopy(kwargs['json'])
            alignment = value['evidence']['sequence_alignment_evidence']
            rows = value['evidence']['observations']
            if not self.enabled:
                self.seed_messages.update({m['source_message_key']:deepcopy(m) for m in value['messages']})
            if self.enabled:
                with SessionLocal() as db:cp = _identity_checkpoint(db,conversation_id=value['conversation_id'])
                rows[1]['content_clean'] = '这款600Pr0适合日常通勤，具体信息可以再看看。'
                rules = shared_adapter('historical_text_alignment')
                proof = rules.build_correspondence(cp,rows,pre_frame_id=alignment['pre_frame_id'],
                    post_frame_id=alignment['post_frame_id'],new_boundary_tokens=shared_adapter('business_viewport_continuity').boundary_tokens_for_observations(rows,committed_only=False))['proof']
                alignment.update(text_correspondence=proof,candidate_alignment_count=proof['candidate_count'])
                if self.tamper and self.tamper.startswith('presend'):
                    alignment['comparison_result']='checkpoint_unique_prefix_with_suffix'
                    for pair in alignment['matched_pairs']:
                        pair['identity_state']='frame_local_unselected'
                        pair.pop('worker_stable_id',None)
                    if self.tamper=='presend_source':value['evidence']['slot_ledger_states'][1]['source_message_key']='wrong-source'
                    if self.tamper=='presend_pending':value['evidence']['slot_ledger_states'][1]['delivery_state']='pending'
                    if self.tamper=='presend_identity':alignment['matched_pairs'][1]['worker_stable_id']='wrong-stable-id'
                    if self.tamper=='presend_role':rows[1]['sender_role']='self'
                if self.tamper=='score':proof['pairs'][1]['scores']['score']+=1
                if self.tamper=='runner':proof.update(candidate_count=2,runner_up_score=0,margin=proof['best_score']);alignment['candidate_alignment_count']=2
                if self.tamper=='frame':proof['post_frame_id']='other'
                if self.tamper=='policy':proof['policy_digest']='0'*64
                if self.tamper=='version':proof['pairs'][1]['effective_text_version']+=1
                if self.tamper=='suffix':alignment['new_suffix_observation_ids']=[]
                if self.tamper=='mapping':alignment['matched_pairs'][0]['pre_index']=1
                if self.tamper=='redelivery':
                    repeated=deepcopy(self.seed_messages[cp['recent_messages'][1]['source_message_key']])
                    repeated['content']=rows[1]['content_clean']
                    repeated['raw_payload']['observation']=deepcopy(rows[1])
                    repeated['message_position']['screen_order']=2
                    value['messages'].insert(0,repeated)
            kwargs['json']=value
            self.request=deepcopy(value)
        response=self.http.post(path,**kwargs)
        if path.endswith('/wechat/messages/ingest'):
            self.last=response
            if self.enabled and not self.tamper and response.status_code==200:
                repeated=self.http.post(path,**kwargs)
                assert repeated.status_code==200,repeated.text
                assert repeated.json()['data']['ingested_count']==0
        return response


@pytest.mark.parametrize('tamper',[None,'score','runner','frame','policy','version','suffix','mapping','suppress_async',
    'presend','presend_source','presend_pending','presend_identity','presend_role','redelivery'])
def test_hc_http_recomputes_then_continues_original_async_once(http_api,monkeypatch,async_generation,tamper):
    class Model:
        def generate_reply_decision(self,**request):
            return AIEngineDecision(decision='send_reply',reply_text='好的，我帮您查一下。',guard_result='pass',raw_payload={})
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',Model)
    transport=HCTransport(http_api);frames=FrameInputHTTP(transport)
    monkeypatch.setattr(api,'client',frames)
    worker,target=api._setup_bound_conversation()
    with SessionLocal() as db:
        conv=db.get(Conversation,target['conversation_id']);conv.status='waiting_user_reply';conv.friend_state='friend_active'
        db.get(Worker,worker['id']).local_lock_summary={'capabilities':{'text_correspondence_version':2}}
        db.commit()
    originals=['唯一开场','这款600Pro适合日常通勤，具体信息可以再看看。','唯一末句']
    # Seed history without scheduling replies; no outstanding seed background
    # jobs may be counted as the continuation under test.
    async_generation['suppress']=True
    keys=sorted([f'hc-seed-{i}' for i in range(4)],key=lambda k:hashlib.sha256(k.encode()).hexdigest()[:12])
    for key,text in zip(keys,originals):api._ingest(worker,target['conversation_id'],key,text)
    before=async_generation['counts']['generated']
    transport.enabled=True
    transport.tamper=None if tamper=='suppress_async' else tamper
    async_generation['suppress']=tamper=='suppress_async'
    if tamper in {None,'presend','suppress_async','redelivery'}:
        api._ingest(worker,target['conversation_id'],keys[-1],'我想看600Plus，预算3万')
        assert transport.last.status_code==200,transport.last.text
        if tamper=='redelivery':
            assert transport.last.json()['data']['ingested_count']==1
            results=transport.last.json()['data']['results']
            assert sorted(r['ingest_result'] for r in results)==['duplicated','ingested']
        until=time.monotonic()+3
        while tamper!='suppress_async' and async_generation['counts']['generated']<before+1 and time.monotonic()<until:time.sleep(.02)
        assert async_generation['counts']['generated']==before+(tamper!='suppress_async')
        assert transport.request['evidence']['sequence_alignment_evidence']['candidate_alignment_count']==1
    else:
        with pytest.raises(AssertionError):api._ingest(worker,target['conversation_id'],keys[-1],'我想看600Plus，预算3万')
        assert transport.last.status_code in {400,409,422},transport.last.text
        assert async_generation['counts']['generated']==before
    if tamper in {None,'presend','redelivery'}:
        # Scheduling/execution is not proof that the async transaction has
        # committed a usable reply task. Wait for that observable result.
        until=time.monotonic()+5
        while time.monotonic()<until:
            with SessionLocal() as db:
                if db.scalar(select(ReplyAction.id)) is not None:break
            time.sleep(.02)
    with SessionLocal() as db:
        rows=list(db.scalars(select(MessageEvent).order_by(MessageEvent.ingested_at)))
        assert [m.content for m in rows[:3]]==originals
        assert len(rows)==(4 if tamper in {None,'presend','suppress_async','redelivery'} else 3)
        assert not list(db.scalars(select(HandoffEvent)))
        actions=list(db.scalars(select(ReplyAction)))
        tasks=list(db.scalars(select(Task).where(Task.task_type=='chat_reply')))
        assert len(actions)==len(tasks)==(1 if tamper in {None,'presend','redelivery'} else 0)
        if tamper in {None,'presend','redelivery'}:
            assert tasks[0].reply_action_id==actions[0].id
            assert actions[0].reply_text=='好的，我帮您查一下。'
