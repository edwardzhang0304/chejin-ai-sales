"""Original draft replay through real Brain/Guard/C3; synthetic reviewer boundary."""
import json
import pytest
from test_hook_business_http import clean
from test_model_query_hook_pipeline import actual_brain
import test_model_query_hook_pipeline as pipeline
from test_program_preparation_pipeline import listed
from apps.wechat_ai_customer_service import llm_config
from test_semantic_fact_routing import ORIGINAL


def test_original_draft_one_semantic_decision_reaches_send_permission(monkeypatch,actual_brain):
    state=actual_brain
    for name,price,desc in [('大众高尔夫1.4T',3.98,'17款'),('别克英朗',3.78,'21款'),('别克威朗',3.68,'19款')]:
        vid=listed(name,price)
        r=pipeline.joined.api.client.put('/api/vehicles/'+vid,json={'customer_description':desc},headers=pipeline.joined.api.HEADERS)
        assert r.status_code==200
    from test_hook_history_business_http import coverage as original
    def coverage(binding):
        v=original(binding)
        for scan in v['scans']:
            for row,text in zip(scan['databases'][0]['pages'][0],['你好','4万以内有什么车？']):row['StrContent']=text
        return v
    monkeypatch.setattr(pipeline,'coverage',coverage)
    def transport(**kw):
        state['wire'].append(kw)
        if kw['model']=='synthetic-reviewer':
            request=json.loads(kw['messages'][1]['content'])['review_request']
            assert request['draft_reply']==ORIGINAL
            assert request['program_fact_findings']['decision']=='semantic_pending'
            value=pipeline.joined.review('vehicle_facts')
        else:
            value=pipeline.joined.fixtures.base_plan()
            value.update(answer_mode='recommend_from_catalog',reply_segments=[ORIGINAL],facts_claimed=[],evidence_used={})
        return {'ok':True,'status':200,'response_text':json.dumps(value,ensure_ascii=False)}
    monkeypatch.setattr(llm_config,'call_llm_request_once_with_wall_timeout',transport)
    _,_,_,bid=pipeline.created(monkeypatch)
    result=pipeline.generate(bid)
    assert result['decision']=='send_reply',result
    assert len(state['wire'])==2
