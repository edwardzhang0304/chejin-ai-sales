"""Local API/Brain/Guard/native ACK regressions with explicitly synthetic transport."""
import json
import test_model_query_hook_pipeline as pipeline
from test_hook_business_http import clean
from test_model_query_hook_pipeline import actual_brain
from apps.wechat_ai_customer_service import llm_config


def test_invalid_risk_repairs_with_exact_contract_then_native_send(monkeypatch, actual_brain):
    original=llm_config.call_llm_request_once_with_wall_timeout
    drafts=[]
    def transport(**kw):
        response=original(**kw)
        value=json.loads(response['response_text'])
        if kw['model']!='synthetic-reviewer' and 'reply_segments' in value:
            if not drafts:
                value['risk']['risk_level']='safe'
            else:
                prompt=kw['messages'][1]['content']
                assert 'risk.risk_level' in prompt
                assert all(level in prompt for level in ['low','medium','high'])
            drafts.append(value)
            response['response_text']=json.dumps(value,ensure_ascii=False)
        return response
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)
    pipeline.native.test_one_native_history_match_settles_original_reply_without_resend('unique',monkeypatch)
    assert len(drafts)==2 and drafts[0]['risk']['risk_level']=='safe' and drafts[1]['risk']['risk_level']=='low'
    assert actual_brain['receipts']  # Clear facts can use the existing program review route.


def test_rereview_sees_previous_request_and_still_uses_current_authority(monkeypatch, actual_brain):
    original=llm_config.call_llm_request_once_with_wall_timeout
    reviewed=[]
    def transport(**kw):
        response=original(**kw)
        if kw['model']!='synthetic-reviewer':
            value=json.loads(response['response_text'])
            if 'reply_segments' in value:
                price=actual_brain['receipt']['recommendations'][0]['price']
                value['reply_segments'].append(f'大约{round(float(price))}万上下。')
                response['response_text']=json.dumps(value,ensure_ascii=False)
        if kw['model']=='synthetic-reviewer':
            request=json.loads(kw['messages'][1]['content'])['review_request'];reviewed.append(request)
            if len(reviewed)==1:
                value=json.loads(response['response_text'])
                value.update(verdict='repair',semantic_errors=['合成表达修订'],repair_instruction='保持当前车价，表达更自然。')
                response['response_text']=json.dumps(value,ensure_ascii=False)
            else:
                assert request['previous_reviews'][0]['repair_instruction']=='保持当前车价，表达更自然。'
                assert request['authority_evidence_summary']==reviewed[0]['authority_evidence_summary']
        return response
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)
    pipeline.native.test_one_native_history_match_settles_original_reply_without_resend('unique',monkeypatch)
    assert len(reviewed)==2
