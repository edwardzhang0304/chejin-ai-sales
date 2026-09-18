"""D1 final HTTP envelope and original async generation; desktop frames are inputs."""
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
from app.models.c3 import Conversation
from app.models.lead import Lead
from app.models.wechat import WechatSessionBinding
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision
from app.services.wechat_service import _identity_checkpoint
from app.contracts.shared_rules import shared_adapter


class Transport:
    def __init__(self, http):
        self.http, self.worker, self.enabled, self.tamper = http, None, False, None
        self.last_response = None
    def get(self, *args, **kwargs):
        return self.http.get(*args, **kwargs)
    def post(self, path, **kwargs):
        if self.enabled and path.endswith('/wechat/messages/ingest'):
            value = deepcopy(kwargs['json'])
            with SessionLocal() as db:
                checkpoint = _identity_checkpoint(db, conversation_id=value['conversation_id'])
            rows = value['evidence']['observations']
            rows[1]['content_clean'] = '般两厢，平时接送孩子'
            alignment = value['evidence']['sequence_alignment_evidence']
            rules = shared_adapter('historical_text_alignment')
            built = rules.build_correspondence(checkpoint, rows,
                pre_frame_id=alignment['pre_frame_id'], post_frame_id=alignment['post_frame_id'],
                new_boundary_tokens=shared_adapter('business_viewport_continuity').boundary_tokens_for_observations(rows, committed_only=False))
            assert built, (checkpoint, rows)
            proof = built['proof']
            if self.tamper == 'score': proof['pairs'][1]['similarity'] = 1
            if self.tamper == 'expired': proof['checkpoint_digest'] = '0'*64
            if self.tamper == 'source': proof['pairs'][1]['source_message_key'] = 'another-customer'
            if self.tamper != 'missing': alignment['text_correspondence'] = proof
            if self.tamper == 'refresh':
                # A real authority update AFTER capture expires the first
                # proof. Only the original Worker's refresh rule may rebuild.
                with SessionLocal() as db:
                    binding = db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id == value['conversation_id']))
                    db.get(Lead,binding.lead_id).customer_name = '张师傅'
                    db.commit()
            kwargs['json'] = value
        response = self.http.post(path, **kwargs)
        if self.enabled and self.tamper == 'refresh' and path.endswith('/wechat/messages/ingest'):
            assert response.status_code == 409, response.text
            assert response.json()['data']['recovery_action'] == 'refresh_and_rebuild'
            from chejin_worker_client.historical_alignment import refreshed_correspondence
            refreshed = self.http.get(f"/api/workers/{self.worker['id']}/wechat/conversations/{kwargs['json']['conversation_id']}/read-authorization",
                headers=api._worker_headers(self.worker))
            assert refreshed.status_code == 200, refreshed.text
            original_messages = deepcopy(kwargs['json']['messages'])
            proof = refreshed_correspondence(kwargs['json'], refreshed.json()['data']['identity_checkpoint'])
            assert proof
            kwargs['json']['evidence']['sequence_alignment_evidence']['text_correspondence'] = proof
            response = self.http.post(path, **kwargs)
            assert kwargs['json']['messages'] == original_messages
        if self.enabled and self.tamper in {None,'refresh'} and path.endswith('/wechat/messages/ingest') and response.status_code == 200:
            repeated = self.http.post(path, **kwargs)
            assert repeated.status_code == 200, repeated.text
            assert repeated.json()['data']['ingested_count'] == 0
        if path.endswith('/wechat/messages/ingest'): self.last_response = response
        return response


@pytest.mark.parametrize('tamper', [None, 'refresh', 'missing', 'score', 'expired', 'source', 'unsupported'])
def test_formal_ingest_validates_same_mapping_without_rewriting_facts(http_api, monkeypatch, async_generation, tamper):
    class Model:
        def generate_reply_decision(self, request):
            return AIEngineDecision(reply_text="好的，我来介绍看车安排。", guard_result="pass", raw_payload={})
    monkeypatch.setattr(c3_service, 'get_ai_engine_adapter', Model)
    transport = Transport(http_api)
    frames = FrameInputHTTP(transport)
    monkeypatch.setattr(api, 'client', frames)
    worker, target = api._setup_bound_conversation()
    transport.worker = worker
    with SessionLocal() as db:
        conversation = db.get(Conversation, target['conversation_id'])
        conversation.status, conversation.friend_state = 'waiting_user_reply', 'friend_active'
        db.commit()
    originals = ['周末带家人出去看看', '一般两厢，平时接送孩子', '顺便看看后备箱空间']
    # The existing fixture derives sequence IDs from a hash. Supply keys in
    # increasing fixture sequence order, like production's monotonic allocator.
    keys = sorted([f'd1-seed-{i}' for i in range(4)], key=lambda k: hashlib.sha256(k.encode()).hexdigest()[:12])
    for key, text in zip(keys, originals): api._ingest(worker, target['conversation_id'], key, text)
    with SessionLocal() as db:
        db.get(Worker, worker['id']).local_lock_summary = {'capabilities': {'text_correspondence_version': 0 if tamper == 'unsupported' else 1}}
        db.commit()
    before = async_generation['counts']['generated']
    transport.enabled, transport.tamper = True, tamper
    if tamper in {None,'refresh'}:
        api._ingest(worker, target['conversation_id'], keys[-1], '一般两厢，平时接送孩子可以吗')
        assert transport.last_response.status_code == 200
        until = time.monotonic() + 3
        while async_generation['counts']['generated'] < before + 1 and time.monotonic() < until:
            time.sleep(.02)
        assert async_generation['counts']['generated'] == before + 1
    else:
        with pytest.raises(AssertionError):
            api._ingest(worker, target['conversation_id'], keys[-1], '一般两厢，平时接送孩子可以吗')
        assert transport.last_response.status_code in {409, 422}, transport.last_response.text
        assert async_generation['counts']['generated'] == before
    with SessionLocal() as db:
        events = list(db.scalars(select(MessageEvent).order_by(MessageEvent.ingested_at)))
        assert [e.content for e in events[:3]] == originals
        assert len(events) == (4 if tamper in {None,'refresh'} else 3)
        if tamper in {None,'refresh'}:
            assert events[-1].content == '一般两厢，平时接送孩子可以吗'
