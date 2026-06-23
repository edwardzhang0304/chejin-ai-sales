"""Focused checks for the reusable live-chat style adapter."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for path in (PROJECT_ROOT, WORKFLOWS_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")

from apps.wechat_ai_customer_service.admin_backend.services.customer_service_settings import CustomerServiceSettings  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import tenant_context, tenant_root  # noqa: E402
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore  # noqa: E402
from knowledge_loader import build_evidence_pack  # noqa: E402
from listen_and_reply import apply_local_customer_service_settings  # noqa: E402
from reply_style_adapter import adapt_reply_style, guard_adapted_reply, infer_source_channel  # noqa: E402
from style_memory_store import retrieve_style_examples  # noqa: E402


TENANT_ID = "chejin"


def main() -> int:
    results = []
    for check in (
        check_retrieves_inline_style_examples,
        check_retrieves_rag_style_hits,
        check_fast_adapter_blends_real_service_style,
        check_style_adapter_uses_recent_context_before_asking_budget_again,
        check_style_adapter_suppresses_repeated_honorific_mid_chat,
        check_style_adapter_avoids_followup_honorific_without_recent_state,
        check_identity_probe_denies_ai_when_guard_enabled,
        check_handoff_reply_is_softened_without_explicit_transfer,
        check_price_and_finance_boundary_is_polite_but_safe,
        check_trade_in_handoff_keeps_specific_context,
        check_appointment_handoff_preempts_trade_in_context,
        check_location_contact_handoff_keeps_specific_context,
        check_new_energy_handoff_not_misclassified_by_mileage,
        check_new_energy_handoff_does_not_invent_commute_distance,
        check_new_energy_over_transfer_does_not_become_same_day_delivery,
        check_guard_rejects_new_protected_numbers,
        check_source_channel_inference_prefers_realtime,
        check_local_settings_can_disable_style_adapter,
        check_chejin_real_pack_is_available_to_adapter,
        check_governed_style_only_rag_fallback_is_available,
    ):
        try:
            results.append({"name": check.__name__, "ok": bool(check())})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
    failures = [item for item in results if not item.get("ok")]
    payload = {"ok": not failures, "count": len(results), "failures": failures, "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def check_retrieves_inline_style_examples() -> bool:
    pack = {
        "evidence": {
            "style_examples": [
                {
                    "id": "style_price_1",
                    "customer_message": "这个价格有点贵",
                    "service_reply": "哥，价格我理解，您主要是预算卡在哪个区间？",
                    "source_type": "cleaned_real_chat_pack",
                    "score": 0.8,
                }
            ]
        }
    }
    examples = retrieve_style_examples("价格能不能低一点", evidence_pack=pack, tenant_id=TENANT_ID)
    return bool(examples) and examples[0]["id"] == "style_price_1" and "预算" in examples[0]["service_reply"]


def check_retrieves_rag_style_hits() -> bool:
    pack = {
        "evidence": {},
        "rag_evidence": {
            "hits": [
                {
                    "chunk_id": "rag_style_1",
                    "category": "chats",
                    "source_type": "cleaned_real_chat_pack",
                    "score": 0.65,
                    "text": "聊天记录：客户：这个车有点贵 客服：哥，这个价确实不是最低，但车况比较稳，您预算大概卡在哪？",
                }
            ]
        },
    }
    examples = retrieve_style_examples("这个车有点贵", evidence_pack=pack, tenant_id=TENANT_ID)
    return bool(examples) and examples[0]["id"] == "rag_style_1" and "客服" not in examples[0]["service_reply"]


def check_fast_adapter_blends_real_service_style() -> bool:
    config = style_config()
    pack = {
        "evidence": {
            "style_examples": [
                {
                    "id": "style_reco_1",
                    "customer_message": "预算十万，推荐几台",
                    "service_reply": "哥，您把预算和用途说下，我按车况帮您缩到两三台。",
                    "source_type": "cleaned_real_chat_pack",
                    "score": 0.9,
                }
            ]
        }
    }
    result = adapt_reply_style(
        config=config,
        customer_message="预算十来万，家用通勤，有没有推荐？",
        reply_text="收到，我先记录一下。您可以看看现有方案。",
        source_channel="realtime",
        evidence_pack=pack,
        recent_reply_texts=[],
    )
    text = str(result.get("reply_text") or "")
    return result.get("applied") is True and "不乱推" not in text and "哥" in text and "9.8万" not in text


def check_style_adapter_uses_recent_context_before_asking_budget_again() -> bool:
    config = style_config()
    pack = {
        "evidence": {
            "style_examples": [
                {
                    "id": "style_reco_no_reask_budget",
                    "customer_message": "预算十万，推荐几台",
                    "service_reply": "哥，您先说下预算、用途和有没有置换，我再按实际情况帮您筛。",
                    "source_type": "cleaned_real_chat_pack",
                    "score": 0.9,
                }
            ]
        }
    }
    result = adapt_reply_style(
        config=config,
        customer_message="那就按刚才说的，直接挑两台，别再问预算了。",
        reply_text="按您现在这个方向，我建议先从Polo和领动里挑。别只看年份，先看公里数、车况记录和检测报告；后面我再帮您把优先级排清楚。",
        source_channel="realtime",
        evidence_pack=pack,
        recent_reply_texts=["从9万以内、主要给您爱人开、自动挡、倒车影像优先这个条件看，可以先筛两台。"],
    )
    text = str(result.get("reply_text") or "")
    forbidden = ("您把预算", "说下预算", "预算大概", "确认一下预算", "预算上限", "您先说下预算", "后面后面")
    return result.get("applied") is True and not any(marker in text for marker in forbidden)


def check_style_adapter_suppresses_repeated_honorific_mid_chat() -> bool:
    config = style_config()
    pack = {
        "evidence": {
            "style_examples": [
                {
                    "id": "style_reco_repeat_guard",
                    "customer_message": "预算十万，推荐几台",
                    "service_reply": "哥，您把预算和用途说下，我按车况帮您缩到两三台。",
                    "source_type": "cleaned_real_chat_pack",
                    "score": 0.9,
                }
            ]
        }
    }
    result = adapt_reply_style(
        config=config,
        customer_message="那这台车还推荐吗？",
        reply_text="收到，我帮您看一下。",
        source_channel="realtime",
        evidence_pack=pack,
        recent_reply_texts=["哥，这台我先按您的预算帮您看。"],
    )
    text = str(result.get("reply_text") or "")
    return result.get("applied") is True and not text.startswith(("哥，", "老板，", "姐，", "老师，"))


def check_style_adapter_avoids_followup_honorific_without_recent_state() -> bool:
    config = style_config()
    pack = {
        "evidence": {
            "style_examples": [
                {
                    "id": "style_reco_followup_guard",
                    "customer_message": "预算十六万，想看SUV",
                    "service_reply": "老板，途观L和奇骏可以先看。",
                    "source_type": "cleaned_real_chat_pack",
                    "score": 0.9,
                }
            ]
        }
    }
    result = adapt_reply_style(
        config=config,
        customer_message="如果要显得稍微体面点接客户，途观L和奇骏你更偏哪台？",
        reply_text="途观L商务感更稳一点，奇骏更偏实用。",
        source_channel="realtime",
        evidence_pack=pack,
        recent_reply_texts=[],
    )
    text = str(result.get("reply_text") or "")
    return result.get("applied") is True and not text.startswith(("哥，", "老板，", "姐，", "老师，"))


def check_identity_probe_denies_ai_when_guard_enabled() -> bool:
    result = adapt_reply_style(
        config=style_config(identity_guard=True),
        customer_message="你是不是AI机器人？",
        reply_text="这个问题我需要转人工处理。",
        source_channel="handoff",
        evidence_pack={},
        recent_reply_texts=[],
        needs_handoff=True,
    )
    text = str(result.get("reply_text") or "")
    return (
        result.get("applied") is True
        and "不是AI" in text
        and "不是机器人" in text
        and "转人工" not in text
        and any(term in text for term in ("别介意", "具体需求", "车况", "核实"))
    )


def check_handoff_reply_is_softened_without_explicit_transfer() -> bool:
    result = adapt_reply_style(
        config=style_config(identity_guard=True),
        customer_message="合同和发票怎么开？",
        reply_text="这个问题需要转人工客服处理。",
        source_channel="handoff",
        evidence_pack={},
        recent_reply_texts=[],
        needs_handoff=True,
    )
    text = str(result.get("reply_text") or "")
    return result.get("applied") is True and "转人工" not in text and any(term in text for term in ("核实", "核清楚", "负责人", "稍等", "确认", "问清楚"))


def check_price_and_finance_boundary_is_polite_but_safe() -> bool:
    result = adapt_reply_style(
        config=style_config(identity_guard=True),
        customer_message="贷款你能不能保证包过？再给我最低价，我现在就定。",
        reply_text="这个问题需要转人工客服处理。",
        source_channel="handoff",
        evidence_pack={},
        recent_reply_texts=[],
        needs_handoff=True,
    )
    text = str(result.get("reply_text") or "")
    return (
        result.get("applied") is True
        and "转人工" not in text
        and any(term in text for term in ("贷款", "金融", "资方", "价格", "成交"))
        and any(term in text for term in ("核准", "核实", "确认", "准话"))
        and not any(term in text for term in ("保证包过", "保证最低价", "一定能批", "绝对最低"))
        and "负责人意见" not in text
        and "争取归争取" not in text
    )


def check_trade_in_handoff_keeps_specific_context() -> bool:
    result = adapt_reply_style(
        config=style_config(identity_guard=True),
        customer_message="我还有台2018年的朗逸想置换，6万多公里，苏州牌，大概流程怎么走？",
        reply_text="这个问题需要转人工客服处理。",
        source_channel="handoff",
        evidence_pack={},
        recent_reply_texts=[],
        needs_handoff=True,
    )
    text = str(result.get("reply_text") or "")
    return (
        result.get("applied") is True
        and "转人工" not in text
        and any(term in text for term in ("置换", "公里", "车况", "检测", "行情", "估"))
        and any(term in text for term in ("大概区间", "粗估", "照片", "配置", "过户", "保养"))
    )


def check_appointment_handoff_preempts_trade_in_context() -> bool:
    result = adapt_reply_style(
        config=style_config(identity_guard=True),
        customer_message="如果置换价合适，我今天下午就过来看车，能先留车吗？",
        reply_text="这个问题需要转人工客服处理。",
        source_channel="handoff",
        evidence_pack={},
        recent_reply_texts=[],
        needs_handoff=True,
    )
    text = str(result.get("reply_text") or "")
    return result.get("applied") is True and any(term in text for term in ("记", "确认", "车源", "排期", "回复"))


def check_location_contact_handoff_keeps_specific_context() -> bool:
    result = adapt_reply_style(
        config=style_config(identity_guard=True),
        customer_message="最后你们门店地址和到了找谁，再帮我确认一下。",
        reply_text="这个问题需要转人工客服处理。",
        source_channel="handoff",
        evidence_pack={},
        recent_reply_texts=[],
        needs_handoff=True,
    )
    text = str(result.get("reply_text") or "")
    return (
        result.get("applied") is True
        and "转人工" not in text
        and any(term in text for term in ("地址", "导航", "到店", "找谁", "联系人", "对接人"))
        and any(term in text for term in ("确认", "核清楚", "核好", "发您", "回您"))
    )


def check_new_energy_handoff_not_misclassified_by_mileage() -> bool:
    result = adapt_reply_style(
        config=style_config(identity_guard=True),
        customer_message="我看秦PLUS DM-i，平时一天来回40公里，电池和三电能保证没问题吗？",
        reply_text="这个问题需要转人工客服处理。",
        source_channel="handoff",
        evidence_pack={},
        recent_reply_texts=[],
        needs_handoff=True,
    )
    text = str(result.get("reply_text") or "")
    return (
        result.get("applied") is True
        and any(term in text for term in ("电池", "三电", "检测", "续航"))
        and any(term in text for term in ("担心", "正常", "关键", "记下"))
        and "40公里" in text
        and "旧车" not in text
    )


def check_new_energy_handoff_does_not_invent_commute_distance() -> bool:
    result = adapt_reply_style(
        config=style_config(identity_guard=True),
        customer_message="我想看插混，三电和电池你们能不能保证后面不出问题？",
        reply_text="这个问题需要转人工客服处理。",
        source_channel="handoff",
        evidence_pack={},
        recent_reply_texts=[],
        needs_handoff=True,
    )
    text = str(result.get("reply_text") or "")
    return (
        result.get("applied") is True
        and any(term in text for term in ("电池", "三电", "检测", "续航"))
        and "40公里" not in text
        and "通勤距离我记下" not in text
        and "每天通勤" not in text
    )


def check_new_energy_over_transfer_does_not_become_same_day_delivery() -> bool:
    result = adapt_reply_style(
        config=style_config(identity_guard=True),
        customer_message="想看新能源，每天通勤70公里，电池能不能保证没问题？异地过户麻烦吗？周日能看车最好。",
        reply_text="这个问题需要转人工客服处理。",
        source_channel="handoff",
        evidence_pack={},
        recent_reply_texts=[],
        needs_handoff=True,
    )
    text = str(result.get("reply_text") or "")
    return (
        result.get("applied") is True
        and any(term in text for term in ("电池", "三电", "检测", "续航"))
        and "70公里" in text
        and not any(term in text for term in ("当天提", "当天办完", "临牌"))
    )


def check_guard_rejects_new_protected_numbers() -> bool:
    guard = guard_adapted_reply(
        base_reply="这台车可以看，具体以门店确认为准。",
        adapted_reply="这台车9.8万，库存2台，可以直接定。",
        customer_message="这台车能看吗？",
        identity_guard=True,
        source_channel="realtime",
    )
    return guard.get("allowed") is False and guard.get("reason") == "new_protected_number_tokens"


def check_source_channel_inference_prefers_realtime() -> bool:
    return infer_source_channel(
        realtime_reply={"applied": True},
        llm_synthesis={"applied": True},
        llm_reply={},
        rag_reply={"applied": True},
    ) == "realtime"


def check_local_settings_can_disable_style_adapter() -> bool:
    tenant_id = "style_adapter_settings_test"
    service = CustomerServiceSettings(tenant_id=tenant_id)
    service.save({"style_adapter_enabled": False})
    with tenant_context(tenant_id):
        config = apply_local_customer_service_settings({"reply_style_adapter": {"enabled": True}, "llm_reply_synthesis": {}})
    return config.get("reply_style_adapter", {}).get("enabled") is False


def check_chejin_real_pack_is_available_to_adapter() -> bool:
    with tenant_context(TENANT_ID):
        pack = build_evidence_pack("价格有点贵，预算十万左右", context={})
    examples = retrieve_style_examples("价格有点贵，预算十万左右", evidence_pack=pack, tenant_id=TENANT_ID)
    return bool(examples) and any(str(item.get("service_reply") or "") for item in examples)


def check_governed_style_only_rag_fallback_is_available() -> bool:
    tenant_id = "style_only_rag_fallback_probe"
    reset_tenant(tenant_id)
    try:
        with tenant_context(tenant_id):
            store = RagExperienceStore()
            record = store.record_reply(
                target="style_only_probe",
                message_ids=["style-only-001"],
                question="最低多少钱？",
                reply_text="这个价格我需要请示负责人确认一下，确认好再给您准确答复。",
                raw_reply_text="这个价格我需要请示负责人确认一下，确认好再给您准确答复。",
                intent_assist={"intent": "price", "recommended_action": "answer"},
                rag_reply={
                    "applied": True,
                    "hit": {
                        "chunk_id": "style-only-chunk",
                        "source_id": "style-only-source",
                        "score": 0.88,
                        "category": "chats",
                        "source_type": "cleaned_real_chat_pack",
                        "text": "客户问最低价，客服请示负责人后再回复。",
                    },
                },
            )
            store.update_metadata(
                record["experience_id"],
                {
                    "source": "real_chat_style",
                    "source_type": "cleaned_real_chat_pack",
                    "ai_interpretation": {
                        "recommended_action": "discard",
                        "auto_triage": {"reason_code": "product_master_facts_must_stay_manual"},
                    },
                    "experience_review": {"status": "auto_kept"},
                },
                rebuild_index=False,
            )
            examples = retrieve_style_examples(
                "这个车最低多少钱",
                evidence_pack={},
                tenant_id=tenant_id,
                limit=2,
                include_rag_style_hits=True,
            )
        return bool(examples) and any(str(item.get("source") or "") == "rag_style_only" for item in examples)
    finally:
        reset_tenant(tenant_id)


def reset_tenant(tenant_id: str) -> None:
    root = tenant_root(tenant_id)
    resolved = root.resolve()
    expected_parent = (APP_ROOT / "data" / "tenants").resolve()
    if expected_parent not in resolved.parents:
        raise RuntimeError(f"refusing to remove unexpected tenant root: {root}")
    if root.exists():
        shutil.rmtree(root)


def style_config(*, identity_guard: bool = True) -> dict[str, Any]:
    return {
        "reply": {"prefix": ""},
        "llm_reply_synthesis": {"identity_guard_enabled": identity_guard},
        "reply_style_adapter": {
            "enabled": True,
            "mode": "fast_local",
            "apply_to_realtime": True,
            "apply_to_rag": True,
            "apply_to_llm": True,
            "apply_to_rule": True,
            "apply_to_handoff": True,
            "identity_guard_aware": True,
            "max_examples": 3,
            "min_similarity": 0.01,
            "include_rag_style_hits": True,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
