"""Synthetic boundary regressions; provider quality is verified separately live."""
from copy import deepcopy
import pytest
from test_program_catalog_preparation import prepared
from test_catalog_query import row
from test_catalog_query_entry import fixtures, review
from apps.wechat_ai_customer_service.workflows import catalog_query as cq
from apps.wechat_ai_customer_service.workflows import customer_service_brain as brain
from apps.wechat_ai_customer_service.workflows import customer_service_quality_reviewer as reviewer


ORIGINAL = ('4万左右可以先看17款大众高尔夫1.4T，3.98万，比较贴近你的预算。'
            '接着是21款别克英朗3.78万，19款别克威朗3.68万，这两台也都在预算内。')


def case(text=ORIGINAL):
    rows = [row('a',3.98,model='高尔夫',customer_description='17款大众高尔夫1.4T'),
            row('b',3.78,model='英朗',customer_description='21款别克英朗'),
            row('c',3.68,model='威朗',customer_description='19款别克威朗')]
    for item, name in zip(rows,['大众高尔夫1.4T','别克英朗','别克威朗']):
        item['data']['name'] = name
    pack, result, inp, runtime = prepared('4万以内有什么车？',rows=rows)
    assert result['route']=='direct'
    p = fixtures.base_plan()
    p.update(answer_mode='recommend_from_catalog',reply_segments=[text],facts_claimed=[],evidence_used={})
    return pack, inp, p, runtime


@pytest.mark.parametrize('text',[ORIGINAL,
    '先看17款大众高尔夫1.4T，3.98万；再看21款别克英朗3.78万和19款别克威朗3.68万，后面两辆也不错。',
    '大众高尔夫1.4T，3.98万，别克英朗3.78万，别克威朗3.68万，都在4万上下。'])
def test_uncertain_natural_language_reaches_existing_reviewer(text):
    pack, inp, p, _ = case(text)
    checked = brain.validate_plan_against_evidence(p,pack)
    assert checked['ok'],checked
    route = cq.prepared_review_route(pack,p)
    assert route.get('needs_semantic_review'),route
    assert '_catalog_clearance' not in pack
    raw = review('vehicle_facts')
    cleared = cq.record_semantic_clearance(pack,p,raw,reviewer.normalize_quality_review_result(raw))
    assert cleared['ok'],cleared
    assert pack['_catalog_clearance']['method']=='semantic'
    assert p['reply_segments']==[text]


@pytest.mark.parametrize('text',[
    '大众高尔夫1.4T报价2万元。',
    '大众高尔夫1.4T报价3.98元。',
])
def test_explicit_wrong_price_cannot_be_overruled_by_model_pass(text):
    pack,_,p,_=case(text)
    assert not brain.validate_plan_against_evidence(p,pack)['ok']
    assert not cq.prepared_review_route(pack,p).get('needs_semantic_review')
    raw=review('vehicle_facts')
    result=cq.record_semantic_clearance(pack,p,raw,reviewer.normalize_quality_review_result(raw))
    assert not result['ok'] and '_catalog_clearance' not in pack


@pytest.mark.parametrize('changes',[
    {'verdict':'repair','semantic_errors':['年款与原始资料不符']},
    {'verdict':'block'}, {'catalog_claim_kind':'uncertain'},
    {'verdict':'pass','semantic_errors':['报价冲突']},
])
def test_uncertain_wording_never_bypasses_negative_or_conflicting_review(changes):
    pack,_,p,_=case()
    raw=review('vehicle_facts',**changes)
    result=cq.record_semantic_clearance(pack,p,raw,reviewer.normalize_quality_review_result(raw))
    assert not result['ok'] and '_catalog_clearance' not in pack


def test_explicit_forged_source_still_blocks_before_model():
    pack,_,p,_=case()
    p['evidence_used']={'product_ids':['forged-id']}
    assert not brain.validate_plan_against_evidence(p,pack)['ok']


def test_explicit_named_price_list_retains_program_order_even_if_model_passes():
    pack,_,p,_=case('别克英朗报价3.78万元，大众高尔夫1.4T报价3.98万元。')
    assert 'reply_recommendation_order_mismatch' in brain.validate_plan_against_evidence(p,pack)['errors']
    raw=review('vehicle_facts')
    assert not cq.record_semantic_clearance(pack,p,raw,reviewer.normalize_quality_review_result(raw))['ok']


@pytest.mark.parametrize('text',[
    '如果大众高尔夫1.4T报价2万元，您就考虑吗？',
    '客户问的是“大众高尔夫1.4T报价2万元？”',
])
def test_non_assertions_do_not_become_program_price_conflicts(text):
    pack,_,p,_=case(text)
    assert brain.validate_plan_against_evidence(p,pack)['ok']
    assert cq.prepared_review_route(pack,p)['needs_semantic_review']
