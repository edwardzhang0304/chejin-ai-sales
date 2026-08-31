from pathlib import Path
import sys


OMNIAUTO_ROOT = Path(__file__).resolve().parents[2] / "worker-client" / "omniauto-rpa"
for path in (
    OMNIAUTO_ROOT,
    OMNIAUTO_ROOT / "apps" / "wechat_ai_customer_service",
    OMNIAUTO_ROOT / "apps" / "wechat_ai_customer_service" / "workflows",
    OMNIAUTO_ROOT / "apps" / "wechat_ai_customer_service" / "adapters",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.admin_backend.services import conversation_history  # noqa: E402
from apps.wechat_ai_customer_service.workflows import customer_service_brain  # noqa: E402
from apps.wechat_ai_customer_service.workflows import reply_evidence_builder  # noqa: E402
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter  # noqa: E402


HISTORY = [
    {
        "id": "old-customer-message",
        "sender_role": "customer",
        "message_type": "text",
        "content": "轿车，市区多",
        "occurred_at": "2026-08-31T00:40:00+08:00",
    },
    {
        "id": "old-sales-reply",
        "sender_role": "sales",
        "message_type": "text",
        "content": "好的，我按市区通勤方向帮您看。",
        "occurred_at": "2026-08-31T00:40:05+08:00",
    },
]


def test_c3_transport_history_reaches_legacy_history_consumers(monkeypatch):
    class EmptyRawMessageStore:
        def list_messages(self, **_kwargs):
            return []

    monkeypatch.setattr(reply_evidence_builder, "RawMessageStore", EmptyRawMessageStore)
    raw_capture = {
        "conversation": {
            "conversation_id": "c3-history-bridge",
            "history": HISTORY,
        }
    }
    current_batch = [
        {
            "id": "current-customer-message",
            "sender_role": "customer",
            "message_type": "text",
            "content": "10万以内",
        }
    ]

    history = reply_evidence_builder.recent_history(
        raw_capture=raw_capture,
        batch=current_batch,
        max_messages=40,
        char_budget=12000,
        history_messages=HISTORY,
    )
    assert [item["content"] for item in history] == [
        "轿车，市区多",
        "好的，我按市区通勤方向帮您看。",
    ]

    assembled = conversation_history.assemble_conversation_history(
        target_name="CJBGPV6A",
        conversation_id="c3-history-bridge",
        current_batch=current_batch,
        history_messages=HISTORY,
    )
    assert "轿车，市区多" in assembled["history_text"]
    assert "好的，我按市区通勤方向帮您看。" in assembled["history_text"]


def test_evidence_pack_reads_c3_history_from_raw_capture(monkeypatch):
    class EmptyRawMessageStore:
        def list_messages(self, **_kwargs):
            return []

    monkeypatch.setattr(reply_evidence_builder, "RawMessageStore", EmptyRawMessageStore)
    monkeypatch.setattr(reply_evidence_builder, "build_evidence_pack", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(reply_evidence_builder, "catalog_product_candidates", lambda *_args, **_kwargs: [])

    evidence_pack = reply_evidence_builder.build_reply_evidence_pack(
        config={"llm_reply_synthesis": {"max_history_messages": 40, "history_char_budget": 12000}},
        target_name="CJBGPV6A",
        target_state={"conversation_context": {}},
        batch=[{"id": "current-customer-message", "sender_role": "customer", "content": "10万以内"}],
        combined="10万以内",
        decision={},
        reply_text="",
        intent_assist={},
        rag_reply={},
        llm_reply={},
        product_knowledge={},
        data_capture={},
        raw_capture={
            "conversation": {
                "conversation_id": "c3-history-bridge",
                "history": HISTORY,
            }
        },
    )

    assert evidence_pack["conversation"]["history_count"] == 2
    assert "轿车，市区多" in evidence_pack["conversation"]["history_text"]
    assert "好的，我按市区通勤方向帮您看。" in evidence_pack["conversation"]["history_text"]


def test_explicit_empty_c3_history_does_not_fall_back_to_legacy_store(monkeypatch):
    class StaleRawMessageStore:
        def list_messages(self, **_kwargs):
            return [{"sender": "旧会话", "content": "不应进入当前上下文"}]

    monkeypatch.setattr(reply_evidence_builder, "RawMessageStore", StaleRawMessageStore)
    raw_capture = {
        "conversation": {
            "conversation_id": "c3-history-bridge",
            "history": [],
        }
    }

    history = reply_evidence_builder.recent_history(
        raw_capture=raw_capture,
        batch=[{"id": "current-customer-message", "content": "10万以内"}],
        max_messages=40,
        char_budget=12000,
        history_messages=reply_evidence_builder._transport_history_from_raw_capture(raw_capture),
    )

    assert history == []


def test_real_adapter_projects_c3_history_into_omniauto_context(monkeypatch):
    adapter = RealOmniAutoAIEngineAdapter()
    captured = {}
    brain_result = {
        "rule_name": "customer_service_brain_reply",
        "adoptable": True,
        "visible_reply_source": "brain_plan.reply_segments",
        "reply_text": "收到，我继续按前面的条件帮您看。",
        "guard_verdict": "pass",
        "brain_plan": {
            "recommended_action": "send_reply",
            "confidence": 0.9,
            "risk_flags": [],
            "evidence_refs": [],
            "reply_segments": ["收到，我继续按前面的条件帮您看。"],
        },
    }

    def fake_run_brain_isolated(**kwargs):
        captured.update(kwargs["invocation"])
        return brain_result

    monkeypatch.setattr(
        adapter,
        "_load_config",
        lambda: {"customer_service_brain": {"provider": "test", "model": "test", "api_key": "test-only"}},
    )
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(adapter, "_run_brain_isolated", fake_run_brain_isolated)

    decision = adapter.generate_reply_decision(
        conversation_context={
            "conversation_id": "c3-history-bridge",
            "remark_code": "CJBGPV6A",
            "history": HISTORY,
        },
        message_batch={
            "id": "batch-history-bridge",
            "messages": [{"id": "current-customer-message", "content": "10万以内"}],
        },
    )

    assert decision.decision == "send_reply"
    projected = captured["target_state"]["conversation_context"]["ledger_recent_messages"]
    assert [item["content"] for item in projected] == [
        "轿车，市区多",
        "好的，我按市区通勤方向帮您看。",
    ]


def test_history_is_present_in_final_brain_prompt_projection(monkeypatch):
    class EmptyRawMessageStore:
        def list_messages(self, **_kwargs):
            return []

    monkeypatch.setattr(reply_evidence_builder, "RawMessageStore", EmptyRawMessageStore)
    raw_capture = {
        "conversation": {
            "conversation_id": "c3-history-bridge",
            "history": HISTORY,
        }
    }
    current_batch = [{"id": "current-customer-message", "sender_role": "customer", "content": "10万以内"}]
    history = reply_evidence_builder.recent_history(
        raw_capture=raw_capture,
        batch=current_batch,
        max_messages=40,
        char_budget=12000,
        history_messages=HISTORY,
    )
    assembled = conversation_history.assemble_conversation_history(
        target_name="CJBGPV6A",
        conversation_id="c3-history-bridge",
        current_batch=current_batch,
        history_messages=HISTORY,
    )
    evidence_pack = {
        "conversation": {
            "context": {},
            "history": history,
            "history_text": assembled["history_text"],
            "current_batch_text": assembled["current_batch_text"],
            "conversation_summary": "",
            "raw_conversation_id": "c3-history-bridge",
        },
        "evidence": {"audit_summary": {}},
    }
    brain_input = customer_service_brain.build_brain_input(
        settings={"mode": "brain_first", "require_final_visible_polish": True, "max_reply_segments": 3},
        target_name="CJBGPV6A",
        target_state={"conversation_context": {"ledger_recent_messages": HISTORY}},
        batch=current_batch,
        combined="10万以内",
        raw_capture=raw_capture,
        evidence_pack=evidence_pack,
    )
    prompt_input = customer_service_brain.slim_brain_input_for_prompt(
        brain_input,
        settings={"history_char_budget": 12000, "summary_char_budget": 360, "current_batch_char_budget": 500},
    )
    assert "轿车，市区多" in prompt_input["conversation"]["history_text"]
    assert "好的，我按市区通勤方向帮您看。" in prompt_input["conversation"]["history_text"]
