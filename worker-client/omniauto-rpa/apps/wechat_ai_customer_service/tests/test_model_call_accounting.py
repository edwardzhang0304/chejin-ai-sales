"""Count actual transport entry attempts and shared budgets, without model bodies."""
import json
import pytest
from test_brain_completion_integrity import brain
from apps.wechat_ai_customer_service import llm_config as llm


def call():
    return llm.call_llm_request_with_failover(provider='openai',api_key='secret-sentinel',base_url='https://synthetic.invalid',model='synthetic',
        messages=[{'role':'user','content':'private-message-sentinel'}],timeout=20,max_tokens=100,progress_stage='brain_llm')


def test_failover_is_two_http_attempts_under_one_logical_call_and_deadline(monkeypatch):
    clock=[100.0];seen=[]
    monkeypatch.setattr(llm.time,'monotonic',lambda:clock[0])
    def transport(**kw):
        seen.append(kw['timeout']);clock[0]+=2
        return {'ok':len(seen)>1,'status':200 if len(seen)>1 else 503,'error_kind':'transport' if len(seen)==1 else None}
    monkeypatch.setattr(llm,'call_llm_request_once_with_wall_timeout',transport)
    monkeypatch.setattr(llm,'resolve_llm_fallback_settings',lambda **kw:{'enabled':True,'provider':'openai','api_key':'fallback-secret','base_url':'https://fallback.invalid','model':'backup'})
    with llm.llm_total_time_budget(5) as audit:
        assert call()['ok']
        with llm.llm_total_time_budget(100) as nested:
            assert nested is audit and llm.llm_total_time_budget_remaining_seconds()==1
    assert seen==[5,3]
    assert audit['logical_call_count']==1 and audit['http_attempt_count']==2
    assert [a['route'] for a in audit['http_attempts']]==['primary','fallback']
    assert {a['parent_call_id'] for a in audit['http_attempts']}=={audit['logical_calls'][0]['call_id']}
    assert audit['elapsed_ms']==4000 and audit['remaining_budget_seconds']==1
    assert 'secret-sentinel' not in json.dumps(audit) and 'private-message-sentinel' not in json.dumps(audit)
    assert llm.llm_total_time_budget_remaining_seconds() is None


def test_exhausted_budget_records_skip_without_http_and_does_not_leak_next_turn(monkeypatch):
    clock=[100.0];monkeypatch.setattr(llm.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(llm,'call_llm_request_once_with_wall_timeout',lambda **kw:pytest.fail('budget must stop transport'))
    with llm.llm_total_time_budget(1) as audit:
        clock[0]+=2
        assert call()['total_time_budget_exhausted']
    assert audit['logical_call_count']==1 and audit['http_attempt_count']==0
    with llm.llm_total_time_budget(1) as next_turn:
        assert next_turn['logical_call_count']==0 and next_turn['turn_id']!=audit['turn_id']


def test_exception_restores_parent_call_and_budget_context(monkeypatch):
    def transport(**kw):raise RuntimeError('synthetic')
    monkeypatch.setattr(llm,'call_llm_request_once_with_wall_timeout',transport)
    with pytest.raises(RuntimeError),llm.llm_total_time_budget(5) as audit:
        call()
    assert audit['logical_call_count']==audit['http_attempt_count']==1
    assert llm._LLM_CALL.get() is None and llm.llm_total_time_budget_remaining_seconds() is None


def test_diagnostic_failure_cannot_change_result_or_disable_progress(monkeypatch):
    events=[]
    monkeypatch.setattr(llm,'call_llm_request_once_with_wall_timeout',lambda **kw:{'ok':True,'status':200})
    monkeypatch.setattr(llm,'_emit_llm_progress_event',lambda **kw:events.append(kw))
    def broken(**kw):raise ValueError('synthetic diagnostics failure')
    monkeypatch.setattr(llm,'_record_http_attempt',broken)
    with llm.llm_total_time_budget(5) as audit:
        assert call()['ok']
    assert [x['event'] for x in events]==['started','finished']
    assert audit['diagnostic_errors']==2 and llm._LLM_CALL.get() is None


@pytest.mark.parametrize('value,category',[
    ({'ok':False,'error_kind':'transport'},'service_unavailable'),
    ({'ok':False,'error_kind':'protocol'},'output_format'),
    ({'ok':False,'error_kind':'catalog_query_validation'},'query_contract'),
    ({'ok':False,'verdict':'repair'},'content_review'),
    ({'ok':False,'error':'catalog_changed_during_generation'},'inventory_changed'),
    ({'ok':False,'error_kind':'catalog_query_execution'},'execution_failure'),
])
def test_failure_categories_keep_technical_and_content_recovery_distinct(value,category):
    assert llm.ai_failure_category(value)==category
    assert llm.requires_technical_recovery(value)==bool(value.get('error_kind'))


@pytest.mark.parametrize('entry',['generation','repair'])
@pytest.mark.parametrize('failure',['none','cutoff','syntax'])
def test_real_generation_and_repair_entries_keep_original_call_limits(monkeypatch,entry,failure):
    from test_brain_completion_integrity import run,response
    good=response();bad=response(cutoff=True,finish='length') if failure=='cutoff' else {**good,'response_text':good['response_text'].replace('"can_answer":','"can_answer"',1)}
    outputs=[good] if failure=='none' else [bad,good]
    wire=[]
    def transport(**kw):
        wire.append(kw);assert outputs,'unexpected extra recovery call'
        return outputs.pop(0)
    monkeypatch.setattr(brain,'resolve_llm_api_key',lambda **kw:'synthetic')
    monkeypatch.setattr(llm,'call_llm_request_once_with_wall_timeout',transport)
    monkeypatch.setattr(llm,'resolve_llm_fallback_settings',lambda **kw:{'enabled':False})
    with llm.llm_total_time_budget(30) as audit:
        assert run(entry)['ok']
    assert not outputs
    assert audit['logical_call_count']==audit['http_attempt_count']==len(wire)==(1 if failure=='none' else 2)
    assert all(x['phase']==('content_repair' if entry=='repair' else 'understanding') for x in audit['logical_calls'])
