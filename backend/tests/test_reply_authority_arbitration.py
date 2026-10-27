"""Synthetic needs/quote contrasts; real contract/reviewer, no live model calls."""
from copy import deepcopy
import pytest
import test_brain_fact_guidance_regression as fixture
from apps.wechat_ai_customer_service.workflows import customer_service_brain as brain
import customer_service_quality_reviewer as reviewer
from customer_service_brain_contract import verify_brain_reply_quality

QUESTION = '车龄2-4年，价格8-12w，无大修，省油的油车'
REPLIES = [
    ['收到，2-4年车龄、8-12万、无大修、省油的油车，我记下了。', '您更偏轿车还是SUV？'],
    ['您的需求记下了：车龄2到4年、预算8到12万、油车、无大修。', '您更偏轿车还是SUV？'],
]


def plan(reply=None):
    return fixture.make_plan(mode='collect_customer_info', reply=reply or REPLIES[0])


def verdict(**changes):
    return dict(status='ok', verdict='pass', confidence=.95, claim_kind='non_factual',
                customer_visible_risk='low', semantic_errors=[], hard_boundary_concerns=[],
                repair_instruction='', reason='Synthetic needs acknowledgement', **changes)


def assess(p, settings=None):
    return brain.validate_runtime_brain_plan(p, settings=settings or {},
        brain_input={'current_message': {'clean_text': QUESTION}},
        evidence_pack={'current_message': QUESTION}, combined=QUESTION)


@pytest.mark.parametrize('reply', REPLIES)
def test_budget_echo_requires_complete_semantic_classification_once(monkeypatch, reply):
    calls=[]; reviewer._REVIEW_CACHE.clear()
    monkeypatch.setattr(reviewer, 'reviewer_from_settings', lambda cfg: calls.append(cfg) or verdict())
    p=plan(reply); original=deepcopy(p)
    assert assess(p)['ok']
    quality=verify_brain_reply_quality(p,current_message=QUESTION,evidence_pack={})
    assert reviewer.review_brain_reply_semantics(settings={},plan=p,
        brain_input={'current_message':{'clean_text':QUESTION}},
        evidence_pack={'current_message':QUESTION},deterministic_quality=quality)['ok']
    assert len(calls)==1
    assert p==original


@pytest.mark.parametrize('bad', [
    {'status':'error','error_kind':'network'},
    {'status':'error','error_kind':'protocol'},
    {'verdict':'pass','customer_visible_risk':'low'},
    {'verdict':'pass','claim_kind':'factual','customer_visible_risk':'low'},
    {'verdict':'pass','claim_kind':'uncertain','customer_visible_risk':'low'},
    {'verdict':'block','claim_kind':'non_factual','customer_visible_risk':'low'},
    {'verdict':'pass','claim_kind':'non_factual','customer_visible_risk':'low','semantic_errors':['wrong interpretation']},
    {'verdict':'pass','claim_kind':'non_factual','customer_visible_risk':'low','hard_boundary_concerns':['unsupported promise']},
])
def test_uncertain_or_failed_review_cannot_relax_missing_authority(monkeypatch,bad):
    reviewer._REVIEW_CACHE.clear()
    monkeypatch.setattr(reviewer,'reviewer_from_settings',lambda cfg:bad)
    assert not assess(plan())['ok']


@pytest.mark.parametrize('settings',[{'semantic_reviewer_enabled':False},{'semantic_reviewer_mode':'shadow'}])
def test_disabled_or_shadow_review_cannot_clear_authority(settings):
    assert not assess(plan(),settings)['ok']


def test_real_quote_at_same_customer_budget_is_not_an_echo(monkeypatch):
    reviewer._REVIEW_CACHE.clear()
    response=verdict();response['claim_kind']='factual'
    monkeypatch.setattr(reviewer,'reviewer_from_settings',lambda cfg:response)
    assert not assess(plan(['这台车售价12万，保证无大修。']))['ok']


def test_explicit_product_mode_and_missing_sources_still_block(monkeypatch):
    def unexpected(cfg): raise AssertionError('Explicit missing facts need no semantic override')
    monkeypatch.setattr(reviewer,'reviewer_from_settings',unexpected)
    p=plan(['这台车售价12万。']);p['answer_mode']='quote_product_fact'
    assert 'missing_fact_claims' in assess(p)['errors']
    p['facts_claimed']=[{'fact_type':'price','source_level':'product_master','source_id':'invented','value':'12万'}]
    assert not brain.validate_plan_against_evidence(p,{})['ok']


@pytest.mark.parametrize('reply',REPLIES)
def test_real_api_child_brain_keeps_budget_reply_without_rewrite(monkeypatch,reply):
    monkeypatch.setattr(fixture,'CANDIDATES',[reply])
    fixture.test_c3_api_database_real_brain_and_provider_preserve_guided_reply(
        monkeypatch,0,False,customer_text=QUESTION,review_claim_kind='non_factual',
        cache_enabled=True,expected_reviewer_calls=1)


def test_changed_draft_must_not_reuse_non_factual_clearance(monkeypatch):
    reviewer._REVIEW_CACHE.clear()
    replies=[verdict(),dict(verdict(),claim_kind="factual")]
    calls=[]
    def respond(cfg):
        calls.append(True)
        return replies[len(calls)-1]
    monkeypatch.setattr(reviewer,"reviewer_from_settings",respond)
    assert assess(plan())["ok"]
    assert not assess(plan(["这台车售价12万，现车随时可提。"]))["ok"]
    assert len(calls)==2
