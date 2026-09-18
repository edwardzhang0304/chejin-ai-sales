"""D4 polarity vectors through the real quality gate; no live model claims."""
from copy import deepcopy

import pytest

import run_customer_service_brain_contract_checks as fixtures
from customer_service_brain_contract import normalize_brain_plan, verify_brain_reply_quality


ERROR = "unverified_cargo_capacity_affirmative_claim"
QUESTION = "秦PLUS后备箱能装下梯子吗？"
SAFE = [
    "不能保证一定能装下梯子，需要实车测量。",
    "无法保证肯定能装下梯子，建议实车测量。",
    "不确定这台车一定能装下梯子，需要实测。",
    "不能保证后备箱肯定能装下梯子，需要实测。",
    "不能保证能装下梯子，建议实车测量。",
    "无法保证这台车能装下梯子，建议实车测量。",
    "不保证能塞下梯子，建议测量后备箱。",
    "不能确定能装下梯子，建议实测。",
    "无法确定能装下梯子，建议实测。",
    "不确定能装下梯子，建议实测。",
    "尚不能确认能装下梯子，建议实测。",
    "未确认能装下梯子，建议实测。",
    "不一定装得下梯子，建议实测。",
    "不能保证，能装下这把梯子。建议实测。",
    "能不能装下梯子，需要实测。",
    "是否能装下梯子，需要实测。",
    "后备箱能否装下梯子，需要实测。",
    "建议带上梯子，核对后备箱开口和实际尺寸。",
    "您问“秦PLUS后备箱能装下梯子吗？”需要实车测量确认。",
]
UNSAFE = [
    "不能保证，肯定能装下梯子。",
    "不能保证，一定能装下梯子。",
    "不能保证一定能装下梯子，不过肯定能装下冰箱。",
    "不是不能保证一定能装下梯子。",
    "肯定能装下梯子。",
    "应该能装下梯子，建议实测。",
    "大概率能塞下梯子，建议实测。",
    "基本能装下梯子，建议实测。",
    "后备箱够装梯子，问题不大，建议实测。",
    "不能保证，但肯定能装下梯子。",
    "不能保证。但是能装下梯子。",
    "不能保证；能装下梯子。",
    "不能保证\n能装下梯子。",
    "不能保证，不过能装下梯子。",
    "不能保证，然而能装下梯子。",
    "不能保证能装下梯子，后备箱够装。",
    "不能保证送车时间，这台能装下梯子。",
    "不是不能装下梯子。",
    "不能说装不下梯子。",
    "不是不确定能装下梯子。",
    "不确定，但后备箱问题不大。",
    "厂家说“肯定能装下梯子”，建议实测。",
    "您问“能装下冰箱吗？”建议测量后备箱。",
    "您问“秦PLUS后备箱能装下梯子吗？”肯定能装下。",
    "能不能装下梯子？肯定能装下。",
]


def plan_for(reply):
    plan = fixtures.base_plan()
    plan.update(answer_mode="direct_answer", facts_claimed=[], reply_segments=[reply])
    plan["evidence_used"]["common_sense_topics"] = ["后备箱尺寸需要实测"]
    return normalize_brain_plan(plan)


def cargo_pack():
    pack = fixtures.fake_evidence_pack(include_product=True)
    pack["current_message"] = QUESTION
    pack["current_batch"] = [{"id": "msg1", "sender": "customer", "content": QUESTION}]
    return pack


@pytest.mark.parametrize("reply", SAFE)
def test_scoped_negative_or_verified_question_is_not_an_affirmative_claim(reply):
    result = verify_brain_reply_quality(plan_for(reply), current_message=QUESTION,
                                       evidence_pack=cargo_pack(), settings={})
    assert ERROR not in result["errors"], result
    assert result["ok"], result


@pytest.mark.parametrize("reply", UNSAFE)
def test_unverified_affirmation_cannot_borrow_an_earlier_negation(reply):
    result = verify_brain_reply_quality(plan_for(reply), current_message=QUESTION,
                                       evidence_pack=cargo_pack(), settings={})
    assert ERROR in result["errors"], result
    assert not result["ok"]


def test_dimensions_keep_existing_authority_rule_and_do_not_mutate_evidence():
    pack = cargo_pack()
    product = pack["knowledge"]["product_master"]["items"][0]
    product["specs"] = "后备箱装载尺寸长180cm、宽100cm、高80cm"
    before = deepcopy(pack)
    result = verify_brain_reply_quality(plan_for("秦PLUS能装下梯子。"), current_message=QUESTION,
                                       evidence_pack=pack, settings={})
    assert ERROR not in result["errors"], result
    assert pack == before


def test_scoped_exception_does_not_disable_original_fact_guards():
    fixtures.check_guard_rejects_unsupported_price_without_product_master()
    fixtures.check_guard_v2_product_conflict_requests_brain_repair()
    fixtures.check_guard_rejects_finance_lowest_down_payment_commitment()
