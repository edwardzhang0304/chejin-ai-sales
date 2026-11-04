"""Real DB/Brain/query/review/Guard/recovery/permit; synthetic model and local inventory."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import sys

import pytest
from sqlalchemy import select
import test_catalog_brain_joined as joined
from apps.wechat_ai_customer_service.workflows import customer_service_brain as brain
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.vehicle import KnowledgeItem
from app.models.c3 import MessageBatch, ReplyAction, HandoffEvent
from app.models.task import Task
from app.services import c3_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from app.services.c3_recovery import recover_due_message_batches_once
reviewer = sys.modules[brain.review_brain_reply_semantics.__module__]


def records(value, key):
    if isinstance(value, dict):
        if key in value: yield value[key]
        for child in value.values(): yield from records(child, key)
    elif isinstance(value, list):
        for child in value: yield from records(child, key)


@pytest.mark.parametrize("mode", ["normal", "oversize", "syntax", "ambiguous", "exhausted", "oversize_completion"])
@pytest.mark.parametrize("network_first", [False, True])
def test_generation_history_survives_catalog_completion_and_database(monkeypatch, mode, network_first):
    joined.api.setup_function(); joined.api._create_listed_vehicle()
    settings = get_settings()
    monkeypatch.setattr(settings, "c3_omniauto_root", str(joined.root / "worker-client/omniauto-rpa"))
    monkeypatch.setattr(settings, "c3_batch_recovery_max_attempts", 2)
    state, calls, reads = {}, [], []
    attempts = []

    class Runtime:
        def __init__(self):
            with SessionLocal() as db:
                self.tenant_id = db.scalar(select(KnowledgeItem.tenant_id).where(KnowledgeItem.category_id == "products"))
        def list_items(self, kind):
            reads.append(kind)
            with SessionLocal() as db:
                return [x.payload for x in db.scalars(select(KnowledgeItem).where(KnowledgeItem.category_id == kind))]

    original = joined.cq.attach_receipt
    def attach(pack, receipt):
        state['receipt'] = receipt
        return original(pack, receipt)
    monkeypatch.setattr(joined.cq, 'attach_receipt', attach)
    monkeypatch.setattr(joined.ev, 'KnowledgeRuntime', Runtime)
    monkeypatch.setattr(joined.legacy_ev, 'build_evidence_pack', lambda *a, **kw: {
        'evidence': {'products': []}, 'safety': {'allowed_auto_reply': True, 'must_handoff': False, 'reasons': []}})
    monkeypatch.setattr(brain, 'resolve_llm_api_key', lambda **kw: 'synthetic')
    monkeypatch.setattr(reviewer, 'resolve_llm_api_key', lambda **kw: 'synthetic')

    def transport(**kw):
        stage = kw['progress_stage']; calls.append((len(attempts), stage))
        if stage == 'semantic_reviewer':
            raw = json.dumps(joined.review('query_summary'))
        elif not state.get('receipt'):
            state['query_calls'] = state.get('query_calls', 0) + 1
            if network_first and state['query_calls'] == 1:
                return {'ok': False, 'status': 503, 'error_kind': 'transport', 'error': 'temporary upstream failure'}
            mid = str(state['invocation']['batch'][-1]['id'])
            q = joined.query(source_ids=[mid]); q['budget'].update(source_ids=[mid], value=200)
            if mode in {'oversize', 'exhausted', 'oversize_completion'}: q['source_ids'] = [mid] * 90
            raw = json.dumps({'catalog_query': q})
            bad = mode != 'normal' and (mode == 'exhausted' or state['query_calls'] == (2 if network_first else 1))
            if bad:
                raw = raw + '{"can_answer":false}' if mode == 'ambiguous' else raw.replace('"catalog_query":', '"catalog_query"', 1)
                state['bad_query'] = raw
        else:
            state['completion_calls'] = state.get('completion_calls', 0) + 1
            receipt = state['receipt']; p = joined.summary_plan(receipt)
            p['facts_claimed'][0]['value'] = receipt['match_count']
            p['reply_segments'] = [f"按本轮预算条件，目前符合条件的在售车辆有{receipt['match_count']}辆。"]
            raw = json.dumps(p, ensure_ascii=False)
            if mode == 'oversize_completion' and state['completion_calls'] == 1:
                raw = raw.replace('"can_answer":', '"can_answer"', 1)
                state['bad_completion'] = raw
        return {'ok': True, 'status': 200, 'response_text': raw, 'response_diagnostics': {'finish_reason': 'stop'}}
    monkeypatch.setattr(brain, 'call_llm_request_with_failover', transport)
    monkeypatch.setattr(reviewer, 'call_llm_request_with_failover', transport)
    config = joined.fixtures.base_config(joined.fixtures.base_plan())
    config['customer_service_brain'].update(mode='brain_first', provider='openai', api_key='synthetic', low_authority_fast_enabled=False, semantic_reviewer_cache_enabled=False)

    class Adapter(RealOmniAutoAIEngineAdapter):
        def _load_config(self): return deepcopy(config)
        def _run_brain_isolated(self, *, config, invocation, timeout_seconds):
            request = json.loads(json.dumps({'config': config, 'invocation': invocation}))
            attempts.append(request); state.clear(); state['invocation'] = request['invocation']
            return json.loads(json.dumps(self._load_brain()(config=request['config'], **request['invocation'])))

    worker, binding, result = joined.flow.generate(monkeypatch, Adapter())
    bid = result['batch']['id']
    with SessionLocal() as db:
        batch = db.get(MessageBatch, bid)
        native = batch.ai_response_snapshot['raw_payload']['omniauto_brain_result']
        due = batch.generated_at + timedelta(seconds=settings.c3_batch_retry_delay_seconds + 1)
        actions = list(db.scalars(select(ReplyAction)))
        tasks = list(db.scalars(select(Task).where(Task.task_type == 'chat_reply')))
    if mode == 'exhausted':
        assert result['decision'] == 'retry_later' and not actions and not tasks and not reads
        stage_audit = native['llm_status']
    else:
        assert result['decision'] == 'send_reply' and len(actions) == len(tasks) == 1
        permit = joined.flow.claim_send(worker, result, joined.flow.claim(worker, binding, result))
        assert permit.status_code == 200
        stage_audit = native['catalog_stages']['understanding']
        assert native['catalog_stages']['completion']['ok'] is True
        assert stage_audit['ok'] is True
    if mode != 'normal':
        audits = list(records(stage_audit, 'repair_input'))
        assert any(a['original_chars'] == len(state['bad_query']) for a in audits)
        expected_sent = len(state['bad_query']) if mode == 'syntax' else 0
        assert any(a['submitted_chars'] == expected_sent and not a['truncated'] for a in audits)
        expected_hash = hashlib.sha256(state['bad_query'].encode()).hexdigest()
        assert expected_hash in list(records(stage_audit, 'response_sha256'))
        # Failed llm_status already carries a bounded legacy raw excerpt. Preserve
        # that existing responsibility; the NEW recovery projections contain none.
        if mode == 'exhausted':
            assert len(stage_audit.get('raw_response_text', '')) <= 1000
        projected_audit = {k: v for k, v in stage_audit.items() if k != 'raw_response_text'} if mode == 'exhausted' else stage_audit
        assert not list(records(projected_audit, 'raw_response_text'))
        assert not list(records(projected_audit, 'response_text'))
    if network_first:
        assert 503 in list(records(stage_audit, 'status'))
        assert 'temporary upstream failure' in list(records(stage_audit, 'error'))
    if mode == 'oversize_completion':
        completed = native['catalog_stages']['completion']
        assert hashlib.sha256(state['bad_completion'].encode()).hexdigest() in list(records(completed, 'response_sha256'))
        assert expected_hash not in list(records(completed, 'response_sha256'))
    if mode == 'normal':
        assert not list(records(stage_audit, 'repair_input'))
    if mode == 'exhausted':
        monkeypatch.setattr(c3_service, 'utcnow', lambda: due)
        recover_due_message_batches_once(); recover_due_message_batches_once()
        with SessionLocal() as db:
            batch = db.get(MessageBatch, bid)
            assert batch.generation_attempt_count == 2
            assert not list(db.scalars(select(ReplyAction)))
            assert not list(db.scalars(select(Task).where(Task.task_type == 'chat_reply')))
            assert len(list(db.scalars(select(HandoffEvent)))) == 1
            assert list(records(batch.ai_response_snapshot, 'repair_input'))
        assert not reads
    expected_query_calls = (1 if mode == 'normal' else 2) + int(network_first)
    for attempt in range(1, len(attempts) + 1):
        assert len([s for a, s in calls if a == attempt and s != 'semantic_reviewer']) == (
            expected_query_calls if mode == 'exhausted' else expected_query_calls + (2 if mode == 'oversize_completion' else 1))
