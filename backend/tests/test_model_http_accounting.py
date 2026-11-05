"""Actual HTTP producer kinds cross shared accounting and reviewer consumers."""
import sys
import pytest
from test_llm_response_diagnostics import http_provider, _response
from apps.wechat_ai_customer_service import llm_config as llm
from apps.wechat_ai_customer_service.workflows import customer_service_brain as brain
reviewer=sys.modules[brain.review_brain_reply_semantics.__module__]


@pytest.mark.parametrize('status',[400,401,403,408,429,500,502,503,504])
def test_provider_http_rejections_keep_status_and_match_reviewer_category(http_provider,monkeypatch,status):
    call,path,wire=http_provider
    monkeypatch.setattr(reviewer,'resolve_llm_api_key',lambda **kw:'synthetic')
    monkeypatch.setattr(llm,'resolve_llm_fallback_settings',lambda **kw:{'enabled':False})
    with llm.llm_total_time_budget(10) as audit:
        result=call({'error':'synthetic provider refusal'},status=status)
        raw=reviewer.run_quality_reviewer_llm(settings={'provider':'openai_compatible','model':'synthetic',
             'base_url':result['base_url'],'timeout_seconds':3},request={'draft_reply':'合成需求澄清'})
        review=reviewer.normalize_quality_review_result(raw)
        terminal=brain.classify_no_visible_reply('brain_quality_verification_failed',payload={
            'quality_gate_v2':review,'last_semantic_review_stage':'quality_gate_v2'})
    assert result['error_kind']==raw['error_kind']==review['error_kind']=='http'
    assert result['status']==raw['http_status']==review['http_status']==status
    assert result['error']==raw['error']==review['reason']
    assert llm.ai_failure_category(result)==llm.ai_failure_category(review)==terminal['failure_category']=='service_unavailable'
    assert not review['ok'] and review['unavailable'] and reviewer.review_requires_recovery(review)
    assert audit['logical_call_count']==audit['http_attempt_count']==wire['calls']==2
    assert all(c['failure_category']=='service_unavailable' for c in audit['logical_calls']+audit['http_attempts'])
    assert all(c['status']==status and c['error_kind']=='http' for c in audit['http_attempts'])


@pytest.mark.parametrize('case,kind,category',[('success',None,'success'),('protocol','protocol','output_format'),('internal','internal','execution_failure')])
def test_http_success_protocol_and_internal_exceptions_remain_distinct(http_provider,monkeypatch,case,kind,category):
    call,path,wire=http_provider
    if case=='internal':
        def broken(**kw):raise RuntimeError('synthetic extractor failure')
        monkeypatch.setattr(llm,'extract_llm_response_text',broken)
    body=b'not-json' if case=='protocol' else _response('synthetic')
    with llm.llm_total_time_budget(10) as audit:
        result=call(body)
    assert result.get('error_kind')==kind and llm.ai_failure_category(result)==category
    assert audit['logical_calls'][0]['failure_category']==audit['http_attempts'][0]['failure_category']==category
    assert audit['http_attempt_count']==wire['calls']==1
    if case!='success':
        review=reviewer.normalize_quality_review_result({**result,'status':'error','http_status':result['status']})
        assert not review['ok'] and not review['unavailable']
        assert llm.ai_failure_category(review)==category
