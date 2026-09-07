import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path('/Users/zhangwentao/Documents/车金/backend/tests')))
from test_brain_fact_guidance_regression import make_plan
from customer_service_brain_contract import validate_brain_plan
from apps.wechat_ai_customer_service.workflows.customer_service_brain import validate_plan_against_evidence,brain_plan_allows_soft_evidence_override

@pytest.mark.parametrize('segments',[
 ['这台售价','8.68万。','您考虑贷款还是全款？'],
 ['您考虑贷款还是全款？','利率','3%。'],
 ['您考虑贷款还是全款？','贷款审批','肯定通过。'],
])
def test_split_claim_is_not_exempt(segments):
    plan=make_plan(reply=segments,mode='collect_customer_info')
    assert 'missing_fact_claims' in validate_brain_plan(plan,require_fact_claims=True)['errors']
    assert not brain_plan_allows_soft_evidence_override(plan)

def test_payment_exemption_never_skips_knowledge_membership():
    plan=make_plan(reply=['好嘞，欢迎您～','您考虑贷款还是全款？'])
    assert validate_brain_plan(plan,require_fact_claims=True)['ok']
    assert not validate_plan_against_evidence(plan,{'evidence_ids':['policy:other-turn']})['ok']

@pytest.mark.parametrize('mode',['recommend_from_catalog','quote_product_fact'])
def test_payment_question_does_not_exempt_factual_mode(mode):
    plan=make_plan(reply=['好嘞，欢迎您～','您考虑贷款还是全款？'],mode=mode)
    assert 'missing_fact_claims' in validate_brain_plan(plan,require_fact_claims=True)['errors']
