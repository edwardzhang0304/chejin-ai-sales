"""Synthetic protocol regressions; live semantic acceptance is recorded separately."""
from copy import deepcopy
import pytest
from test_program_catalog_preparation import prepared, brain, reviewer
from apps.wechat_ai_customer_service.workflows import customer_service_brain_contract as contract
from apps.wechat_ai_customer_service.workflows import catalog_query as cq
from apps.wechat_ai_customer_service.workflows.chejin_brain_context_bridge import canonical_prior_message, ChejinBrainContextError


def test_risk_contract_and_precise_repair_share_allowed_values():
    pack, _, inp, _ = prepared()
    plan = {'can_answer':True,'understanding':{},'answer_mode':'direct_answer','reply_strategy':{},
            'reply_segments':['请问您想了解哪一台？'],'recommended_action':'send_reply',
            'risk':{'risk_level':'safe','risk_tags':[],'needs_handoff':False,'handoff_reason':''}}
    norm = contract.normalize_brain_plan(plan,evidence_pack=pack)
    validation = contract.validate_brain_plan(norm)
    assert validation['errors']==['invalid_draft_field:risk']
    field, = validation['field_errors']
    assert field['field']=='risk.risk_level' and field['actual']=='safe'
    assert field['expected']['enum']==list(contract.RISK_LEVELS)
    feedback = brain.plan_validation_repair_feedback(validation,combined='就第一辆')
    repair = brain.build_brain_repair_prompt_pack(settings={},brain_input=inp,plan=norm,quality=feedback)
    assert 'risk.risk_level' in feedback['repair_instruction']
    for value in contract.RISK_LEVELS:
        assert value in repair['response_schema'] and value in feedback['repair_instruction']
        plan['risk']['risk_level']=value
        assert contract.validate_brain_plan(contract.normalize_brain_plan(plan,evidence_pack=pack))['ok']
    assert norm['risk']['risk_level']=='safe'  # No silent downgrade.


@pytest.mark.parametrize('field,value',[('risk_level',None),('risk_level',1),('needs_handoff','false'),('risk_tags','none'),('handoff_reason',False)])
def test_each_risk_field_reports_its_exact_path(field,value):
    risk={'risk_level':'low','risk_tags':[],'needs_handoff':False,'handoff_reason':''};risk[field]=value
    assert contract.risk_field_errors(risk)[0]['field']=='risk.'+field


def history_row():
    return {'message_event_id':'sent1','sender_role':'self','message_type':'text',
            'content':'先看25款合成乙，再看21款合成甲。',
            'confirmed_reply':{'action':'a','batch':'b','native':'n','vehicles':['a','b'],
                               'vehicle_labels':{'a':'21款合成甲','b':'25款合成乙'}}}


def test_confirmed_text_preserves_order_independently_of_id_list():
    row=canonical_prior_message(history_row())
    state={'chejin_brain_context':{'conversation_id':'c','history':[row]}}
    ids=cq.trusted_vehicle_ids(state,[],'c')
    assert ids['b'][0]['vehicle_name']=='25款合成乙'
    assert ids['b'][0]['reply_text'].startswith('先看25款合成乙')
    assert not cq.trusted_vehicle_ids(state,[],'other')
    state['chejin_brain_context']['history'].append({'sender_role':'self','content':'稍后联系'})
    assert not cq.trusted_vehicle_ids(state,[],'c')


def test_unconfirmed_labels_cannot_introduce_new_vehicle():
    row=history_row();row['confirmed_reply']['vehicle_labels']['not-sent']='伪造车'
    with pytest.raises(ChejinBrainContextError,match='invalid_confirmed_vehicle_labels'):
        canonical_prior_message(row)
    row=history_row();row['confirmed_reply'].pop('vehicle_labels')
    assert 'vehicle_labels' not in canonical_prior_message(row)['confirmed_reply']


def test_rereview_gets_prior_instruction_only_for_same_authority():
    pack,_,inp,_=prepared()
    plan={'reply_segments':['已经按要求修改。']}
    first=reviewer.build_quality_review_request(settings={},brain_input=inp,evidence_pack=pack,plan=plan,deterministic_quality={})
    assert not first['previous_reviews']
    pack['review_history']=[{'authority_sha256':pack['reply_authority_snapshot']['sha256'],
                            'request':first,'raw_result':{'status':'ok','verdict':'repair','repair_instruction':'删除无依据的库存结论。'}}]
    second=reviewer.build_quality_review_request(settings={},brain_input=inp,evidence_pack=pack,plan=plan,deterministic_quality={})
    assert second['previous_reviews'][0]['repair_instruction']=='删除无依据的库存结论。'
    assert 'request' not in second['previous_reviews'][0]
    pack['review_history'][0]['authority_sha256']='other'
    assert not reviewer.previous_review_context(pack)
    pack['review_history'][0]['authority_sha256']=pack['reply_authority_snapshot']['sha256']
    pack['review_history'][0]['raw_result']['status']='timeout'
    assert not reviewer.previous_review_context(pack)
