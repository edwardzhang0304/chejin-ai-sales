"""The bounded existing history may shrink; total work and late-attempt identity may not."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from app.services import c3_service as service
from app.services.ai_adapter import AIEngineDecision
from test_llm_response_diagnostics import http_provider, _response


def batch():
    return SimpleNamespace(ai_response_snapshot={},created_at=datetime.now(timezone.utc),generation_capacity_wait_count=0)


def response():
    return {'decision':'send_reply','raw_payload':{'generation_measurement':{'duration_ms':42},
        'omniauto_brain_result':{'model_call_accounting':{'logical_call_count':3,'http_attempt_count':4}}}}


def test_totals_survive_bounded_history_out_of_order_late_arrival_and_duplicate():
    b=batch()
    for number in [1,3,4,5,6,7,8]:
        b.ai_response_snapshot=service._record_generation_attempt(b,attempt=number,payload=response())
    saved=deepcopy(b.ai_response_snapshot)
    assert len(saved['generation_attempt_history'])==5
    late=service._stale_generation_diagnostics_payload(AIEngineDecision(**response()))
    b.ai_response_snapshot=service._record_stale_generation_attempt_diagnostics(b,attempt=2,payload=late)
    totals=b.ai_response_snapshot['generation_accounting_totals']
    assert totals['recorded_attempt_count']==8 and totals['logical_call_count']==24 and totals['http_attempt_count']==32
    assert totals['generation_duration_ms']==336 and totals['accounted_attempt_ranges']==[[1,8]]
    assert totals['call_counts_complete'] and b.ai_response_snapshot['decision']=='send_reply'
    repeated=service._record_stale_generation_attempt_diagnostics(b,attempt=2,payload=late)
    assert repeated['generation_accounting_totals']['logical_call_count']==24


def test_old_history_and_killed_process_have_incomplete_not_zero_cost():
    b=batch()
    b.ai_response_snapshot={'generation_attempt_history':[{'attempt':1,'response':{'raw_payload':{}}}]}
    progress=[{'event':'started','call_id':'http1','parent_call_id':'logical1'},
              {'event':'started','call_id':'http1','parent_call_id':'logical1'},
              {'event':'started','call_id':'http2','parent_call_id':'logical1'}]
    payload={'raw_payload':{'provider_error':{'provider_progress':progress}}}
    b.ai_response_snapshot=service._record_generation_attempt(b,attempt=2,payload=payload)
    totals=b.ai_response_snapshot['generation_accounting_totals']
    assert totals['recorded_attempt_count']==2 and totals['logical_call_count']==1 and totals['http_attempt_count']==2
    assert not totals['call_counts_complete']
    b.ai_response_snapshot=service._record_generation_attempt(b,attempt=3,payload=response())
    assert not b.ai_response_snapshot['generation_accounting_totals']['call_counts_complete']



def test_accounting_parent_phase_and_budget_cross_actual_http_progress_file(http_provider):
    from apps.wechat_ai_customer_service import llm_config
    from app.services.ai_adapter import _read_provider_progress
    call,path,wire=http_provider
    with llm_config.llm_total_time_budget(10) as audit, llm_config.llm_stage_context('completion'):
        assert call(_response('synthetic'))['ok']
    events=_read_provider_progress(path,progress_id='diag-test')
    assert wire['calls']==audit['http_attempt_count']==audit['logical_call_count']==1
    assert len(events)==2
    assert {e['parent_call_id'] for e in events}=={audit['logical_calls'][0]['call_id']}
    assert all(e['phase']=='completion' and 0<=e['remaining_budget_seconds']<=10 for e in events)
    assert events[-1]['status']==200 and events[-1]['elapsed_ms']>=0
    assert audit['http_attempts'][0]['usage']['completion_tokens']==23
