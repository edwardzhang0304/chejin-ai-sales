"""Knowledge retrieval retains history; current safety uses its own input.

These are evidence/contract checks with artificial text. The separate Provider
and Worker recovery tests exercise actual frozen MessageEvent snapshots.
"""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from app.services.knowledge_management_service import build_retrieval_index
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter

# Use the application's runtime-path bootstrap before importing its exported
# evidence builder; this loads no provider or credentials.
RealOmniAutoAIEngineAdapter._load_context_bridge()
from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import (
    apply_chejin_knowledge_release,
    ChejinKnowledgeProjectionError,
)


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _target():
    items = [{"item_id": "history-policy", "revision_id": "revision-1",
              "title": "青岚交付资料", "content": "办理青岚交付需提供身份证明；具体交付以正式批准为准。"}]
    index = build_retrieval_index(items)
    return {"chejin_knowledge_required": True, "chejin_knowledge_release": {
        "release_id": "release-1", "version": "KR-TEST", "items": items,
        "snapshot_sha256": _digest(items), "retrieval_index": index,
        "retrieval_index_sha256": _digest(index),
    }}


@pytest.mark.parametrize("history", [
    "客服：车辆资料需要核实。", "客服：我们会确认一下。", "客服：可向顾问了解。",
    "客服：人工确认之前不能承诺合同优惠。",
])
@pytest.mark.parametrize("current", ["你好", "买二手车要注意哪些事情？", "要准备什么？", ""])
def test_historical_words_do_not_become_current_handoff_intent(history, current):
    target = _target(); original = deepcopy(target)
    query = history + "\n客户：请介绍青岚交付资料。\n客户：" + current
    pack = {}
    apply_chejin_knowledge_release(pack, target, query_text=query, current_query_text=current)
    assert "handoff" not in pack["intent_tags"]
    assert "handoff_intent_detected" not in pack["safety"]["reasons"]
    # The current elliptical question alone cannot retrieve this item. The
    # original history still supplies the retrieval anchor and exact version.
    faq = pack["knowledge"]["formal_knowledge"]["faq"]
    assert [item["source_id"] for item in faq] == ["knowledge:history-policy@revision-1"]
    assert "身份证明" in faq[0]["answer"]
    assert pack["knowledge"]["intent_tags"] == pack["intent_tags"]
    assert pack["knowledge"]["safety"] == pack["safety"]
    assert target == original


@pytest.mark.parametrize("current", [
    "请转人工", "我要找人工顾问", "要签合同盖章", "请帮我核实", "确认一下",
])
def test_current_risk_inputs_keep_the_existing_review_rules(current):
    pack = {}
    apply_chejin_knowledge_release(pack, _target(), query_text="客服：欢迎咨询。\n客户：" + current,
                                  current_query_text=current)
    assert "handoff" in pack["intent_tags"]
    assert "handoff_intent_detected" in pack["safety"]["reasons"]
    assert pack["safety"]["must_handoff"]


def test_omitted_optional_input_preserves_exported_caller_contract():
    implicit, explicit = {}, {}
    target = _target(); query = "客服：青岚资料需要核实。"
    apply_chejin_knowledge_release(implicit, target, query_text=query)
    apply_chejin_knowledge_release(explicit, target, query_text=query, current_query_text=None)
    assert implicit == explicit
    assert "handoff_intent_detected" in implicit["safety"]["reasons"]


def test_current_input_does_not_bypass_invalid_formal_release():
    target = _target()
    target["chejin_knowledge_release"]["items"][0]["content"] = "未授权的修改"
    with pytest.raises(ChejinKnowledgeProjectionError, match="digest_mismatch"):
        apply_chejin_knowledge_release({}, target, query_text="青岚", current_query_text="你好")


def test_no_managed_release_keeps_optional_integration_unchanged():
    pack = {"safety": {"must_handoff": True, "reasons": ["existing_external_policy"]}}
    original = deepcopy(pack)
    apply_chejin_knowledge_release(pack, {}, query_text="历史", current_query_text="当前")
    assert pack == original
