"""Focused checks for guarded LLM reply synthesis.

These checks are offline and do not call DeepSeek. They use manual JSON
candidates to prove the new synthesis layer can participate in the workflow,
that RAG evidence is passed to the LLM prompt, and that RAG-only evidence cannot
authorize sensitive commitments.
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
WORKFLOWS_ROOT = APP_ROOT / "workflows"
ADAPTERS_ROOT = APP_ROOT / "adapters"
for path in (PROJECT_ROOT, WORKFLOWS_ROOT, APP_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "1")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")
os.environ.setdefault("WECHAT_VPS_BASE_URL", "http://localhost:8000")

from apps.wechat_ai_customer_service.knowledge_paths import shared_runtime_snapshot_path  # noqa: E402
from apps.wechat_ai_customer_service import llm_config as llm_config_module  # noqa: E402

import llm_reply_synthesis as synthesis_module  # noqa: E402
from customer_service_loop import load_rules  # noqa: E402
from listen_and_reply import ReplyDecision, load_config, parse_targets, process_target, resolve_path  # noqa: E402
from llm_reply_guard import guard_synthesized_reply  # noqa: E402
from llm_reply_synthesis import build_synthesis_prompt_pack, maybe_synthesize_reply, normalize_advisor_synthesis_reply  # noqa: E402


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _ensure_cloud_snapshot() -> tuple[Path, bytes | None]:
    snapshot_path = shared_runtime_snapshot_path()
    previous = snapshot_path.read_bytes() if snapshot_path.exists() else None
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    minimal = {
        "schema_version": 1,
        "source": "cloud_official_shared_library",
        "tenant_id": "default",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": future,
        "cache_policy": {"expires_at": future},
        "policy_bundle": {
            "merged": {
                "platform_safety_rules": {
                    "schema_version": 1,
                    "title": "Unit test visible safety rules",
                    "description": "测试用安全边界",
                    "prompt_rules": [
                        {
                            "id": "evidence_first",
                            "title": "先看可见证据",
                            "description": "正式知识、商品库和共享公共知识优先，不能靠代码里的隐藏行业假设。",
                            "instruction": "优先使用客户自己的正式知识、商品库、商品专属问答/规则/解释和共享公共知识；行业、商品、流程和专属规则只能来自 evidence_pack，不要假设客户所属行业。",
                            "enabled": True,
                        },
                        {
                            "id": "rag_participation",
                            "title": "RAG参与理解",
                            "description": "AI经验池可以辅助理解和表达，但不能单独授权敏感承诺。",
                            "instruction": "AI经验池必须积极参与理解和表达；如果使用了RAG，请在used_evidence里写入rag:chunk_id。RAG可以帮助解释、归纳、补充话术，但不能单独授权价格、库存、审批、合同、商品状态保证、售后赔付等敏感承诺。",
                            "enabled": True,
                        },
                        {
                            "id": "no_fabricated_facts",
                            "title": "不编造关键事实",
                            "description": "涉及金额、库存、时效、审批、合同、售后等只能按证据回答。",
                            "instruction": "不能编造价格、库存、商品状态、审批结果、合同条款、售后承诺、最低价或任何正式知识里没有授权的保证。价值、成本、月供、费用、时效、概率等数字只能引用证据包已有数字；没有数字时用定性表述并说明需核实。",
                            "enabled": True,
                        },
                        {
                            "id": "human_only_actions",
                            "title": "人工动作不自动承诺",
                            "description": "预约、留货、订金、合同、审批、人员联系等默认转人工。",
                            "instruction": "普通自动回复的下一步问题优先询问预算、用途、城市、数量、规格、偏好或缺少的关键资料；不要自行承诺预约、预留、试用、到店、上门、订金、合同、审批或人员联系。需要这些人工动作时，recommended_action 必须用 handoff 或 handoff_for_approval，除非正式知识明确授权AI自动处理。",
                            "enabled": True,
                        },
                        {
                            "id": "forbidden_workarounds",
                            "title": "禁止违规绕路",
                            "description": "私下转账、个人账户、发票异常、非法绕路、越界金融建议等必须拒绝。",
                            "instruction": "私下转账、个人账户收款、发票多开少开或金额造假、违法绕路、客户业务之外的股票/金融建议、保证审批通过等是硬边界。不要提供绕路办法，应礼貌说明系统不能自动处理并转人工或拒绝。",
                            "enabled": True,
                        },
                        {
                            "id": "safe_refusal_wording",
                            "title": "拒绝要自然",
                            "description": "拒绝风险要求时，不机械套模板，不复述危险承诺。",
                            "instruction": "拒绝不安全要求时，不要逐字复述危险承诺；应概括为保证、赔付、审批结果、发票/合同处理、锁价、私下付款等边界问题，再说明需要人工核实或系统不能自动处理。",
                            "enabled": True,
                        },
                        {
                            "id": "natural_reply_style",
                            "title": "像真人客服",
                            "description": "回答要短、具体、自然，避免机械化表达。",
                            "instruction": "回复必须拟人化、自然、像真实微信客服在打字，严禁机械化或模板化表达。具体要求：1）语气自然，带适当的口语化衔接，如“好的”“没问题”“是这样”“对的”等，像真人在微信里聊天；2）同一场景下的话术要有变化，不要每次用完全相同的句式开头或结尾；3）2到5句，先接住客户具体需求，再给依据或建议，最后只问一个自然的下一步问题；4）不要固定以“收到，我先记录一下”“稍后继续处理”“这个问题需要人工确认”等套话开头；5）除非确实需要转人工，也要写得具体自然，不要生硬转折。",
                            "enabled": True,
                        },
                        {
                            "id": "multi_message_context",
                            "title": "合并连续短消息",
                            "description": "用户连续追问时按整体意图回答。",
                            "instruction": "如果客户连续发送几条短消息，把它们视为一个合并意图；可以根据历史上下文解析“刚才那台”“这个车”“预算不变”等指代，不要只看最后一句。",
                            "enabled": True,
                        },
                        {
                            "id": "irrelevant_or_out_of_scope",
                            "title": "无关或越界转人工",
                            "description": "完全无关、证据不足或越界问题不能硬答。",
                            "instruction": "完全无关、证据不足、敏感政策或越界问题，recommended_action 用 handoff 或 handoff_for_approval，reply里简短说明系统暂时无法处理并转人工；不要为了显得聪明而猜测。",
                            "enabled": True,
                        },
                        {
                            "id": "custom_visible_prompt_rule",
                            "title": "Custom rule",
                            "instruction": "自定义可见规则：不要承诺测试专用词。",
                            "enabled": True,
                        },
                    ],
                    "guard_terms": {
                        "authority_tags": ["quote", "discount", "stock", "shipping", "invoice", "payment", "after_sales", "handoff", "customer_data"],
                        "commitment_terms": ["包过", "保证通过", "绝对", "最低价", "一口价保证", "保证无", "保证没有", "无风险保证", "肯定没问题", "一定可以", "测试专用硬承诺"],
                        "caution_terms": ["人工", "确认", "核实", "核一下", "问下", "帮您问", "帮您确认", "同事", "顾问", "销售", "转人工", "不能", "不保证", "没法", "需要看", "以检测报告为准", "以合同为准", "目前资料", "记录显示", "暂时不能", "无法直接"],
                        "formulaic_handoff_terms": ["收到，我先记录", "稍后继续处理", "请示上级", "这个问题需要销售人工确认，我先帮您记录并提醒同事跟进", "我先帮您记录并提醒同事跟进", "当前无法直接确认，我先帮您记录"],
                        "forbidden_reply_terms": ["私下转账", "转到个人", "个人账户收款", "多开发票", "少开发票", "发票金额随便", "发票金额可以改", "审批包过", "保证审批", "炒股", "股票建议"],
                        "forbidden_safe_markers": ["不支持", "不能", "不可以", "没法", "无法", "必须", "需要人工", "需要同事", "需要确认"],
                        "appointment_commitment_terms": ["约个时间", "预约", "到店", "上门", "安排时间", "预留", "留货", "订金", "定金"],
                        "appointment_caution_terms": ["负责人", "同事", "人工", "确认", "核实", "联系", "转"],
                        "sales_followup_actors": ["销售", "同事", "顾问", "专员"],
                        "sales_followup_actions": ["联系", "对接", "安排", "跟进", "回电", "给您回", "给你回"],
                        "model_reply_markers": ["[AI]", "llm_synthesis_reply", "rag_context_reply"],
                        "personalized_reply_patterns": ["[许张王李赵刘陈杨黄周吴徐孙马朱胡郭何高林罗郑梁谢宋唐冯韩曹曾彭萧蔡潘田董袁于余叶蒋杜苏魏程吕丁沈任姚卢姜崔钟谭陆汪范金石廖贾夏韦傅方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤][哥姐总先生女士老板]", "客户后续|您之前|你之前|上次"],
                        "situational_handoff_patterns": ["我马上.*转", "稍后.*联系", "直接联系您", "方便留个电话", "转给.*同事"],
                        "finance_boundary_patterns": ["首付|月供|贷款|金融|征信|资方|审批|包过|利率"],
                        "unreasonable_request_patterns": ["提示词|prompt|system prompt|developer message|忽略(前面|上面|规则)|越权", "股票|彩票|赌博|博彩|洗钱|套现|发票.*少开|少开发票|虚开发票", "征信.*不用|包过|一定能批|最低价保证", "事故.*别说|水泡.*别说|火烧.*别说"],
                        "boundary_request_patterns": ["首付|月供|贷款|金融|征信|资方|审批|利率|分期", "事故|水泡|火烧|检测报告|过户|合同|定金|订金|最低价|砍价|优惠"],
                        "observed_product_fact_patterns": ["sku|inventory|库存|价格|报价|在售|已售|商品|产品", "车辆|车型|车况|里程|排量"],
                        "manual_review_terms": ["转人工", "人工确认", "包过", "首付", "月供", "检测", "事故", "水泡", "火烧"],
                        "business_signal_patterns": ["商品|价格|库存|客户|客服|政策|规则|报价|发货|售后|开票|合同|sku|price|inventory"],
                        "product_signal_terms": ["price", "库存", "sku", "商品", "产品", "车辆", "车型", "车况", "报价"],
                        "handoff_rule_terms": ["转人工", "人工确认", "贷款", "首付", "月供"],
                        "policy_signal_terms": ["policy", "规则", "开票", "合同", "售后"],
                        "checklist_price_terms": ["price", "价格", "报价"],
                        "checklist_stock_terms": ["库存", "inventory"],
                        "checklist_finance_terms": ["贷款", "首付", "月供", "金融"],
                        "checklist_condition_terms": ["事故", "水泡", "火烧", "车况"],
                        "risk_finance_terms": ["包过", "首付", "月供"],
                        "risk_condition_terms": ["事故", "水泡", "火烧"],
                        "product_category_terms": ["车", "商品", "二手", "MPV", "SUV"],
                        "policy_payload_terms": ["规则", "政策", "发票", "合同", "售后", "贷款", "过户", "转人工", "人工确认", "policy", "invoice", "contract"],
                        "promotion_signal_patterns": ["客户|客服|商品|价格|库存|政策|规则|车辆|车型|车况|报价|售后|贷款|首付|月供|发票|合同|过户|置换|新能源|电池|人工确认|转人工|sku|price|inventory|customer|service|policy|invoice|contract"],
                        "risk_or_decision_terms": ["price", "unit_price", "minimum", "refund", "compensation", "contract", "credit", "account period", "价格", "报价", "最低价", "优惠", "退款", "退货", "赔偿", "合同", "账期", "月结", "先发货"],
                    },
                },
                "platform_understanding_rules": {
                    "schema_version": 1,
                    "title": "平台通用理解词典",
                    "intent_keywords": {"greeting": ["你好"], "quote": ["价格"]},
                    "intent_groups": {"business": ["quote"]},
                    "policy_type_to_intent": {"invoice": "invoice"},
                    "policy_tags": {"invoice": "invoice_policy"},
                    "policy_type_tags": {"invoice": "invoice_policy"},
                    "policy_key_tags": {},
                    "product_knowledge_keywords": {},
                    "semantic_equivalents": {},
                    "rag": {},
                    "risk_keywords": {},
                    "customer_data_field_labels": {},
                    "quantity_units": ["个", "件", "台"],
                },
            }
        },
    }
    snapshot_path.write_text(json.dumps(minimal, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot_path, previous


def _restore_cloud_snapshot(snapshot_path: Path, previous: bytes | None) -> None:
    if previous is None:
        try:
            snapshot_path.unlink()
        except FileNotFoundError:
            pass
        return
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_bytes(previous)


CONFIG_PATH = APP_ROOT / "configs" / "file_transfer_smoke.example.json"


@dataclass
class FakeConnector:
    messages: list[dict[str, Any]]

    def __post_init__(self) -> None:
        self.sent_texts: list[str] = []

    def get_messages(self, target: str, exact: bool = True) -> dict[str, Any]:
        return {"ok": True, "target": target, "exact": exact, "messages": self.messages}

    def send_text_and_verify(self, target: str, text: str, exact: bool = True, *, skip_send_rate_guard: bool = False) -> dict[str, Any]:
        self.sent_texts.append(text)
        return {"ok": True, "verified": True, "target": target, "exact": exact, "text": text}


def main() -> int:
    result = run_checks()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def run_checks() -> dict[str, Any]:
    snapshot_path, previous_snapshot = _ensure_cloud_snapshot()
    checks = [
        check_synthesis_applies_inside_process_target,
        check_rag_evidence_is_explicit_prompt_material,
        check_synthesis_prompt_is_domain_neutral,
        check_platform_safety_rules_are_visible_and_configurable,
        check_rag_only_authority_topic_forces_handoff,
        check_safe_llm_handoff_wording_is_preserved,
        check_shadow_mode_does_not_apply,
        check_deepseek_flash_pro_routing_and_cost_audit,
        check_synthesis_primary_can_failover_to_backup_provider,
        check_synthesis_visible_reply_surface_cleanup,
        check_guard_allows_safe_store_visit_advisory_but_blocks_commitment,
        check_conflicting_llm_price_falls_back_to_product_master_candidate,
    ]
    results = []
    try:
        for check in checks:
            try:
                check()
                results.append({"name": check.__name__, "ok": True})
            except Exception as exc:
                results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
    finally:
        _restore_cloud_snapshot(snapshot_path, previous_snapshot)
    failures = [item for item in results if not item.get("ok")]
    return {"ok": not failures, "count": len(results), "failures": failures, "results": results}


def check_synthesis_applies_inside_process_target() -> None:
    config = load_test_config()
    config["llm_reply_synthesis"] = {
        "enabled": True,
        "provider": "manual_json",
        "candidate": {
            "can_answer": True,
            "reply": "这台商用冷柜适合小店放饮料，现有资料能确认基础型号和用途；如果您要价格和发货时间，我再按正式资料帮您核对。",
            "confidence": 0.86,
            "recommended_action": "send_reply",
            "needs_handoff": False,
            "used_evidence": ["product:commercial_fridge_bx_200", "faq:scene_product"],
            "rag_used": False,
            "structured_used": True,
            "uncertain_points": ["具体发货时间需按下单城市确认"],
            "risk_tags": [],
            "reason": "natural scene mapped to formal product evidence",
        },
    }
    rules = load_rules(resolve_path(config.get("rules_path")))
    target = parse_targets(config)[0]
    connector = FakeConnector(
        [
            {
                "id": "natural-1",
                "type": "text",
                "content": "我开个小店，想找个能放饮料的冷柜，别太复杂，有没有合适的？",
                "sender": "self",
            }
        ]
    )
    event = process_target(
        connector=connector,  # type: ignore[arg-type]
        target=target,
        config=config,
        rules=rules,
        state={"version": 1, "targets": {}},
        send=False,
        write_data=False,
        allow_fallback_send=False,
        mark_dry_run=False,
    )
    synthesis = event.get("llm_reply_synthesis", {}) or {}
    assert_true(synthesis.get("applied"), "manual synthesis should apply inside process_target")
    assert_equal(event.get("decision", {}).get("rule_name"), "llm_synthesis_reply", "decision should be updated by synthesis")
    assert_true("小店放饮料" in event.get("decision", {}).get("reply_text", ""), "final reply should use synthesized natural text")


def check_rag_evidence_is_explicit_prompt_material() -> None:
    pack = synthetic_pack(intent_tags=["scene_product"], structured=True, rag=True)
    prompt = build_synthesis_prompt_pack(pack)
    payload = json.dumps(prompt["user"], ensure_ascii=False)
    assert_true("rag:rag_chunk_used_car_family" in payload or "rag_chunk_used_car_family" in payload, "prompt should include RAG chunk id")
    assert_true("家庭用车经验" in payload, "prompt should include RAG text")
    assert_true("AI经验池必须积极参与" in json.dumps(prompt, ensure_ascii=False), "prompt rules should emphasize RAG participation")


def check_synthesis_prompt_is_domain_neutral() -> None:
    prompt = build_synthesis_prompt_pack(synthetic_pack(intent_tags=["scene_product"], structured=True, rag=True))
    payload = json.dumps({"system": prompt["system"], "rules": prompt["user"]["rules"]}, ensure_ascii=False)
    forbidden = ["二手车微信销售场景", "车况", "水泡", "火烧", "试驾", "置换"]
    hits = [term for term in forbidden if term in payload]
    assert_true(not hits, f"generic synthesis prompt should not hard-code tenant domain terms: {hits}")
    assert_true("不要假设客户所属行业" in payload, "prompt should explicitly forbid hidden industry assumptions")


def check_platform_safety_rules_are_visible_and_configurable() -> None:
    prompt = build_synthesis_prompt_pack(synthetic_pack(intent_tags=["scene_product"], structured=True, rag=True))
    payload = json.dumps(prompt["user"], ensure_ascii=False)
    assert_true("自定义可见规则" in payload, "prompt should include visible platform safety rules from cloud snapshot")
    guard = guard_synthesized_reply(
        candidate={
            "can_answer": True,
            "reply": "我可以测试专用硬承诺，没问题。",
            "confidence": 0.9,
            "recommended_action": "send_reply",
            "needs_handoff": False,
            "used_evidence": ["product:test"],
            "rag_used": False,
            "structured_used": True,
            "uncertain_points": [],
            "risk_tags": [],
            "reason": "custom platform rule test",
        },
        evidence_pack=synthetic_pack(intent_tags=["scene_product"], structured=True, rag=False),
        settings={},
    )
    assert_equal(guard.get("reason"), "unsafe_commitment_without_caution", "custom visible guard term should be enforced")


def check_rag_only_authority_topic_forces_handoff() -> None:
    original_builder = synthesis_module.build_reply_evidence_pack
    try:
        synthesis_module.build_reply_evidence_pack = lambda **kwargs: synthetic_pack(
            intent_tags=["quote"],
            structured=False,
            rag=True,
        )
        result = maybe_synthesize_reply(
            config={
                "llm_reply_synthesis": {
                    "enabled": True,
                    "provider": "manual_json",
                    "candidate": {
                        "can_answer": True,
                        "reply": "根据经验这台车还能优惠很多，我可以直接给您最低价。",
                        "confidence": 0.91,
                        "recommended_action": "send_reply",
                        "needs_handoff": False,
                        "used_evidence": ["rag:rag_chunk_used_car_family"],
                        "rag_used": True,
                        "structured_used": False,
                        "uncertain_points": [],
                        "risk_tags": ["price_sensitive"],
                        "reason": "rag only price answer",
                    },
                    "require_structured_for_authority": True,
                }
            },
            target_name="文件传输助手",
            target_state={},
            batch=[],
            combined="这车最低多少钱？",
            decision=ReplyDecision("", "no_rule_matched", False, False, "no_rule_matched"),
            reply_text="",
            intent_assist={},
            rag_reply={},
            llm_reply={},
            product_knowledge={},
            data_capture={},
            raw_capture={},
        )
    finally:
        synthesis_module.build_reply_evidence_pack = original_builder
    assert_true(not result.get("applied"), "unsafe authority synthesis must not become a visible guard handoff reply")
    assert_true(result.get("guard", {}).get("action") == "repair", "RAG-only authority answer should request repair")
    assert_equal(
        result.get("guard", {}).get("reason"),
        "authority_topic_without_structured_evidence",
        "guard should explain that formal evidence is required",
    )


def check_safe_llm_handoff_wording_is_preserved() -> None:
    original_builder = synthesis_module.build_reply_evidence_pack
    try:
        synthesis_module.build_reply_evidence_pack = lambda **kwargs: synthetic_pack(
            intent_tags=["payment", "quote"],
            structured=True,
            rag=True,
            must_handoff=True,
        )
        result = maybe_synthesize_reply(
            config={
                "llm_reply_synthesis": {
                    "enabled": True,
                    "provider": "manual_json",
                    "candidate": {
                        "can_answer": False,
                        "reply": "贷款能不能批、最低价能不能锁，我这边不能直接替销售和金融同事拍板；我先把您的预算和车型意向记下，让同事按资料给您准话。",
                        "confidence": 0.81,
                        "recommended_action": "handoff",
                        "needs_handoff": True,
                        "used_evidence": ["product:chejin_camry_2021_20g", "rag:rag_chunk_used_car_family"],
                        "rag_used": True,
                        "structured_used": True,
                        "uncertain_points": ["贷款审批", "最低成交价"],
                        "risk_tags": ["finance", "price_sensitive"],
                        "reason": "safe guarded handoff wording",
                    },
                    "require_structured_for_authority": True,
                }
            },
            target_name="文件传输助手",
            target_state={},
            batch=[],
            combined="你直接保证贷款包过，再给我锁最低价。",
            decision=ReplyDecision("", "no_rule_matched", False, False, "no_rule_matched"),
            reply_text="",
            intent_assist={},
            rag_reply={},
            llm_reply={},
            product_knowledge={},
            data_capture={},
            raw_capture={},
        )
    finally:
        synthesis_module.build_reply_evidence_pack = original_builder
    assert_true(not result.get("applied"), "legacy safe handoff wording should not bypass Brain repair under Guard V2")
    assert_true(result.get("guard", {}).get("action") == "repair", "handoff wording should be reviewed as Brain repair feedback")
    assert_true("guard_handoff_ack" != str(result.get("guard", {}).get("customer_visible_reply_source") or ""), "guard must not write visible handoff text")


def check_shadow_mode_does_not_apply() -> None:
    original_builder = synthesis_module.build_reply_evidence_pack
    try:
        synthesis_module.build_reply_evidence_pack = lambda **kwargs: synthetic_pack(
            intent_tags=["scene_product"],
            structured=True,
            rag=True,
        )
        result = maybe_synthesize_reply(
            config={
                "llm_reply_synthesis": {
                    "enabled": True,
                    "provider": "manual_json",
                    "shadow_mode": True,
                    "candidate": {
                        "can_answer": True,
                        "reply": "可以先看凯美瑞，家用比较均衡。",
                        "confidence": 0.88,
                        "recommended_action": "send_reply",
                        "needs_handoff": False,
                        "used_evidence": ["product:chejin_camry_2021_20g", "rag:rag_chunk_used_car_family"],
                        "rag_used": True,
                        "structured_used": True,
                        "uncertain_points": [],
                        "risk_tags": [],
                        "reason": "shadow test",
                    },
                }
            },
            target_name="文件传输助手",
            target_state={},
            batch=[],
            combined="家用省心有推荐吗？",
            decision=ReplyDecision("", "no_rule_matched", False, False, "no_rule_matched"),
            reply_text="",
            intent_assist={},
            rag_reply={},
            llm_reply={},
            product_knowledge={},
            data_capture={},
            raw_capture={},
        )
    finally:
        synthesis_module.build_reply_evidence_pack = original_builder
    assert_true(not result.get("applied"), "shadow mode should not change final reply")
    assert_equal(result.get("reason"), "shadow_mode", "shadow mode reason should be explicit")
    assert_true(result.get("guard", {}).get("action") == "send_reply", "shadow mode should still run guard")


def check_deepseek_flash_pro_routing_and_cost_audit() -> None:
    original_builder = synthesis_module.build_reply_evidence_pack
    original_read_secret = synthesis_module.read_secret
    original_post = synthesis_module.post_deepseek_synthesis
    captured: list[dict[str, Any]] = []

    def fake_read_secret(name: str) -> str:
        return "unit-test-key" if name == "DEEPSEEK_API_KEY" else ""

    def fake_post(**kwargs: Any) -> dict[str, Any]:
        captured.append(dict(kwargs))
        prompt = kwargs.get("prompt_pack") or {}
        payload = prompt.get("user") if isinstance(prompt.get("user"), dict) else {}
        evidence = payload.get("evidence_pack") if isinstance(payload.get("evidence_pack"), dict) else {}
        must_handoff = bool((evidence.get("safety") or {}).get("must_handoff"))
        candidate = {
            "can_answer": not must_handoff,
            "reply": "这条回复来自受控模型路由测试，普通问题可直接回答；风险问题会转人工确认。",
            "confidence": 0.86,
            "recommended_action": "handoff" if must_handoff else "send_reply",
            "needs_handoff": must_handoff,
            "used_evidence": evidence.get("evidence_ids", []),
            "rag_used": bool(((evidence.get("rag") or {}).get("hits"))),
            "structured_used": bool((evidence.get("audit_summary") or {}).get("structured_evidence_count")),
            "uncertain_points": ["需要人工确认"] if must_handoff else [],
            "risk_tags": ["manual_review"] if must_handoff else [],
            "reason": "unit test model routing candidate",
        }
        return {
            "ok": True,
            "provider": "deepseek",
            "status": 200,
            "response_text": json.dumps(candidate, ensure_ascii=False),
            "usage": {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168},
        }

    base_config = {
        "llm_reply_synthesis": {
            "enabled": True,
            "provider": "deepseek",
            "model_routing": {
                "enabled": True,
                "default_tier": "flash",
                "flash_model": "deepseek-v4-flash",
                "pro_model": "deepseek-v4-pro",
            },
            "cost_controls": {"enabled": True, "max_llm_calls_per_run": 0},
            "require_structured_for_authority": True,
        }
    }

    def call_with_pack(pack: dict[str, Any], text: str) -> dict[str, Any]:
        synthesis_module.build_reply_evidence_pack = lambda **kwargs: pack
        return maybe_synthesize_reply(
            config=base_config,
            target_name="文件传输助手",
            target_state={},
            batch=[],
            combined=text,
            decision=ReplyDecision("", "no_rule_matched", False, False, "no_rule_matched"),
            reply_text="",
            intent_assist={},
            rag_reply={},
            llm_reply={},
            product_knowledge={},
            data_capture={},
            raw_capture={},
        )

    try:
        synthesis_module.read_secret = fake_read_secret
        synthesis_module.post_deepseek_synthesis = fake_post
        normal = call_with_pack(synthetic_pack(intent_tags=["scene_product"], structured=True, rag=True), "家用省心有推荐吗")
        risky = call_with_pack(
            synthetic_pack(intent_tags=["payment", "quote"], structured=True, rag=True, must_handoff=True),
            "你直接保证贷款包过再锁最低价",
        )
    finally:
        synthesis_module.build_reply_evidence_pack = original_builder
        synthesis_module.read_secret = original_read_secret
        synthesis_module.post_deepseek_synthesis = original_post

    assert_equal(normal.get("model_tier"), "flash", "normal reply synthesis should use Flash")
    assert_equal(normal.get("model"), "deepseek-v4-flash", "normal reply synthesis should select flash model")
    assert_true((normal.get("llm_usage") or {}).get("total_tokens") == 168, "usage should be kept for cost audit")
    assert_true((normal.get("prompt_estimate") or {}).get("rough_prompt_tokens", 0) > 0, "prompt estimate should be recorded")
    assert_equal(risky.get("model_tier"), "pro", "risky authority synthesis should use Pro")
    assert_equal(risky.get("model"), "deepseek-v4-pro", "risky authority synthesis should select pro model")
    assert_true(len(captured) == 2, "fake DeepSeek should have been called exactly twice")


def check_synthesis_primary_can_failover_to_backup_provider() -> None:
    old_path = llm_config_module._LLM_CONFIG_PATH
    old_urlopen = llm_config_module.urllib.request.urlopen

    class FakeResponse:
        status = 200

        def __init__(self, body: dict[str, Any]) -> None:
            self.body = body

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.body, ensure_ascii=False).encode("utf-8")

    calls: list[str] = []
    with tempfile.TemporaryDirectory(prefix="omniauto-llm-failover-") as temp_dir:
        llm_config_module._LLM_CONFIG_PATH = Path(temp_dir) / "llm_config.json"
        llm_config_module.save_llm_config(
            {
                "LLM_FALLBACK_ENABLED": "1",
                "LLM_FALLBACK_PROVIDER": "anthropic",
                "LLM_FALLBACK_BASE_URL": "https://aiself.vip/v1",
                "LLM_FALLBACK_FLASH_MODEL": "kimi-for-coding",
                "LLM_FALLBACK_PRO_MODEL": "kimi-for-coding",
                "LLM_FALLBACK_API_KEY": "sk-fallback-kimi",
            }
        )

        def fake_urlopen(request: Any, timeout: int = 0, **kwargs: Any) -> FakeResponse:
            calls.append(str(request.full_url))
            if str(request.full_url).endswith("/chat/completions"):
                body = io.BytesIO(b'{"error":"upstream unavailable"}')
                raise urllib.error.HTTPError(str(request.full_url), 503, "Service Unavailable", hdrs=None, fp=body)
            if str(request.full_url).endswith("/messages"):
                return FakeResponse({"content": [{"type": "text", "text": "{\"reply\":\"好的\",\"confidence\":0.91}"}]})
            raise AssertionError(f"unexpected url: {request.full_url}")

        llm_config_module.urllib.request.urlopen = fake_urlopen
        try:
            result = synthesis_module.post_deepseek_synthesis(
                provider="openai",
                api_key="sk-primary-openai",
                base_url="https://aiself.vip/v1",
                model="gpt-5.4",
                tier="flash",
                prompt_pack={
                    "system": "你是测试助手，只输出JSON。",
                    "user": {"task": "reply"},
                    "response_schema": {"type": "object"},
                },
                timeout=5,
                max_tokens=120,
                temperature=0.1,
            )
        finally:
            llm_config_module.urllib.request.urlopen = old_urlopen
            llm_config_module._LLM_CONFIG_PATH = old_path

    assert_true(result.get("ok") is True, "fallback provider should rescue transient primary failure")
    assert_equal(result.get("provider"), "anthropic", "fallback should switch provider to anthropic")
    assert_true((result.get("failover") or {}).get("activated") is True, "result should record activated failover")
    assert_equal(calls, ["https://aiself.vip/v1/chat/completions", "https://aiself.vip/v1/messages"], "request order should be primary then fallback")


def check_synthesis_visible_reply_surface_cleanup() -> None:
    evidence_pack = {
        "current_message": "这台多少钱？",
        "knowledge": {
            "evidence": {
                "products": [
                    {"id": "p_test", "name": "2019款测试车A", "price": 12.8},
                ],
                "catalog_candidates": [
                    {"id": "p_test", "name": "2019款测试车A", "price": 12.8},
                ],
            }
        },
    }
    cleaned = normalize_advisor_synthesis_reply(
        "先给您短结论：优先我在2019款测试车A（12.8万）、2019款测试车A（12.8万）。我会先看我在测试车A；我在测试车A、另一台先当备选。我在2019款测试车A、偶尔高速先放后面当备选，重点核车况。",
        evidence_pack=evidence_pack,
        settings={"advisor_mode": "direct_question_resolution"},
    )
    assert_true(cleaned.count("2019款测试车A（12.8万）") == 1, cleaned)
    assert_true("我会先看我在" not in cleaned, cleaned)
    assert_true("；我在" not in cleaned, cleaned)
    assert_true("优先我在" not in cleaned, cleaned)
    assert_true("我在2019款测试车A" not in cleaned, cleaned)
    compare_cleaned = normalize_advisor_synthesis_reply(
        "要说省心，优先凯美瑞，雅阁放第二。家用加偶尔高速，凯美瑞一般更偏稳定、好打理。",
        evidence_pack={"current_message": "我在凯美瑞和雅阁之间纠结，主要家用，偶尔跑高速，哪个更省心？"},
        settings={"advisor_mode": "clear_common_sense_recommendation"},
    )
    assert_true("我在" not in compare_cleaned and "先放后面" not in compare_cleaned, compare_cleaned)


def check_guard_allows_safe_store_visit_advisory_but_blocks_commitment() -> None:
    evidence_pack = synthetic_pack(intent_tags=["product_inquiry"], structured=True, rag=False, must_handoff=False)
    safe = guard_synthesized_reply(
        candidate={
            "can_answer": True,
            "reply": "凯美瑞可以优先看，到店先核车况、检测报告和实车状态，再决定要不要试驾。",
            "confidence": 0.86,
            "recommended_action": "send_reply",
            "needs_handoff": False,
            "used_evidence": ["product:chejin_camry_2021_20g"],
            "structured_used": True,
            "rag_used": False,
        },
        evidence_pack=evidence_pack,
        settings={"require_evidence": False},
    )
    assert_true(safe.get("action") == "send_reply", safe)

    commitment = guard_synthesized_reply(
        candidate={
            "can_answer": True,
            "reply": "周六到店我给您约好了，直接过来就行。",
            "confidence": 0.86,
            "recommended_action": "send_reply",
            "needs_handoff": False,
            "used_evidence": ["product:chejin_camry_2021_20g"],
            "structured_used": True,
            "rag_used": False,
        },
        evidence_pack=evidence_pack,
        settings={"require_evidence": False},
    )
    assert_true(commitment.get("action") == "repair", commitment)
    assert_true(commitment.get("hard_boundary") is True, commitment)
    assert_true(str(commitment.get("reason") or "") == "appointment_or_reservation_commitment_requires_handoff", commitment)


def check_conflicting_llm_price_falls_back_to_product_master_candidate() -> None:
    original_builder = synthesis_module.build_reply_evidence_pack
    try:
        synthesis_module.build_reply_evidence_pack = lambda **kwargs: synthetic_pack(
            intent_tags=["product", "quote"],
            structured=True,
            rag=False,
            must_handoff=False,
        )
        result = maybe_synthesize_reply(
            config={
                "llm_reply_synthesis": {
                    "enabled": True,
                    "provider": "manual_json",
                    "candidate": {
                        "can_answer": True,
                        "reply": "这台凯美瑞13万，车况透明。",
                        "confidence": 0.9,
                        "recommended_action": "send_reply",
                        "needs_handoff": False,
                        "used_evidence": ["product:chejin_camry_2021_20g"],
                        "rag_used": False,
                        "structured_used": True,
                        "uncertain_points": [],
                        "risk_tags": [],
                    },
                    "fallback_to_existing_reply": True,
                    "require_structured_for_authority": True,
                }
            },
            target_name="文件传输助手",
            target_state={},
            batch=[],
            combined="凯美瑞多少钱？",
            decision=ReplyDecision("", "no_rule_matched", False, False, "no_rule_matched"),
            reply_text="2021款丰田凯美瑞2.0G豪华版（13.98万），这是当前商品库公开标价。",
            intent_assist={},
            rag_reply={},
            llm_reply={},
            product_knowledge={},
            data_capture={},
            raw_capture={},
        )
    finally:
        synthesis_module.build_reply_evidence_pack = original_builder
    assert_true(result.get("applied"), result)
    assert_true(not result.get("needs_handoff"), result)
    assert_true("13.98万" in str(result.get("raw_reply_text") or ""), result)
    assert_true("guard_rejected_llm_used_existing_reply:product_price_conflicts_with_product_master" == str(result.get("reason") or ""), result)


def synthetic_pack(*, intent_tags: list[str], structured: bool, rag: bool, must_handoff: bool = False) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "products": [],
        "faq": [],
        "policies": {},
        "product_scoped": [],
        "style_examples": [],
    }
    evidence_ids: list[str] = []
    if structured:
        evidence["products"].append({"id": "chejin_camry_2021_20g", "name": "2021款丰田凯美瑞2.0G豪华版", "price": 13.98, "stock": 1})
        evidence["faq"].append({"intent": "family_car", "answer": "适合家庭通勤，最终车况以检测报告为准。"})
        evidence_ids.extend(["product:chejin_camry_2021_20g", "faq:family_car"])
    rag_hits = []
    if rag:
        rag_hits.append(
            {
                "chunk_id": "rag_chunk_used_car_family",
                "source_id": "rag_source_family",
                "score": 0.74,
                "category": "chats",
                "source_type": "rag_experience",
                "product_id": "chejin_camry_2021_20g",
                "text": "家庭用车经验：客户重视省心、油耗和接送孩子时，可以优先解释空间、保养和检测报告。",
            }
        )
        evidence_ids.append("rag:rag_chunk_used_car_family")
    return {
        "schema_version": 1,
        "current_message": "自然问题",
        "conversation": {"history": [{"sender": "customer", "content": "预算十来万，家用省心。"}], "history_count": 1},
        "knowledge": {
            "intent_tags": intent_tags,
            "evidence": evidence,
            "rag_evidence": {"hits": rag_hits, "confidence": 0.74, "rag_can_authorize": False, "structured_priority": True},
            "safety": {"must_handoff": must_handoff, "allowed_auto_reply": not must_handoff, "reasons": ["manual_test"] if must_handoff else []},
        },
        "intent_tags": intent_tags,
        "safety": {"must_handoff": must_handoff, "allowed_auto_reply": not must_handoff, "reasons": ["manual_test"] if must_handoff else []},
        "rag": {"hits": rag_hits, "confidence": 0.74},
        "evidence_ids": evidence_ids,
        "audit_summary": {"structured_evidence_count": 2 if structured else 0, "rag_hit_count": len(rag_hits), "evidence_ids": evidence_ids},
    }


def load_test_config() -> dict[str, Any]:
    config = copy.deepcopy(load_config(CONFIG_PATH))
    config.setdefault("operator_alert", {})["enabled"] = False
    config.setdefault("raw_messages", {})["enabled"] = False
    config.setdefault("rag_response", {})["enabled"] = False
    config.setdefault("intent_assist", {})["enabled"] = False
    return config


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    raise SystemExit(main())
