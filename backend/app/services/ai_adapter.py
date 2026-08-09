from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
import subprocess
from pathlib import Path
import sys
from typing import Protocol

from app.core.config import get_settings
from app.errors import AppError


@dataclass(frozen=True)
class AIEngineDecision:
    decision: str
    reply_text: str | None = None
    confidence: float | None = None
    handoff_reason_code: str | None = None
    risk_flags: list[str] | None = None
    evidence_refs: list[str] | None = None
    guard_result: str | None = None
    rewrite_required: bool = False
    error_code: str | None = None
    suggested_action: str | None = None
    hard_opt_out_evidence: dict | None = None
    raw_payload: dict | None = None


class OmniAutoAIEngineAdapter(Protocol):
    def generate_reply_decision(self, *, conversation_context: dict, message_batch: dict) -> AIEngineDecision:
        """Return a structured service-side decision. The adapter must not mutate business state."""


class MockOmniAutoAIEngineAdapter:
    """Development adapter for C3 workflow tests. Not a formal delivery substitute for real AI Engine."""

    HANDOFF_KEYWORDS = ("人工", "销售电话", "底价", "最低价", "事故", "泡水", "火烧", "贷款", "定金", "合同", "退款", "投诉")

    def generate_reply_decision(self, *, conversation_context: dict, message_batch: dict) -> AIEngineDecision:
        messages = message_batch.get("messages") or []
        combined = "\n".join(str(item.get("content") or "") for item in messages).strip()
        trigger_type = str(message_batch.get("trigger_type") or "customer_message")
        if not combined and trigger_type in {"friend_welcome", "recall"}:
            return AIEngineDecision(
                decision="send_reply",
                reply_text="您好，我是车金的售前顾问。您最近更关注哪类车型？",
                confidence=0.8,
                risk_flags=[],
                evidence_refs=[f"mock_{trigger_type}"],
                guard_result="pass",
                raw_payload={"adapter": "mock", "trigger_type": trigger_type},
            )
        if not combined:
            return AIEngineDecision(
                decision="no_action",
                confidence=0.0,
                guard_result="pass",
                error_code="MESSAGE_BATCH_EMPTY",
                suggested_action="wait_more",
                raw_payload={"adapter": "mock"},
            )
        if "无证据" in combined or "RAG_NO_EVIDENCE" in combined:
            return AIEngineDecision(
                decision="handoff",
                confidence=0.2,
                handoff_reason_code="RAG_NO_EVIDENCE",
                risk_flags=["rag_no_evidence"],
                evidence_refs=[],
                guard_result="handoff",
                error_code="RAG_NO_EVIDENCE",
                suggested_action="handoff",
                raw_payload={"adapter": "mock"},
            )
        if any(keyword in combined for keyword in self.HANDOFF_KEYWORDS):
            return AIEngineDecision(
                decision="handoff",
                confidence=0.7,
                handoff_reason_code="HANDOFF_REQUIRED",
                risk_flags=["manual_handoff_keyword"],
                evidence_refs=["guard_keyword_policy"],
                guard_result="handoff",
                error_code="HANDOFF_REQUIRED",
                suggested_action="handoff",
                raw_payload={"adapter": "mock"},
            )
        if "不回复" in combined or "先看看" in combined:
            return AIEngineDecision(
                decision="no_action",
                confidence=0.65,
                guard_result="pass",
                evidence_refs=["conversation_policy"],
                suggested_action="no_action",
                raw_payload={"adapter": "mock"},
            )
        return AIEngineDecision(
            decision="send_reply",
            reply_text="可以，我先帮您记录需求。您主要看几万预算、轿车还是 SUV？",
            confidence=0.86,
            risk_flags=[],
            evidence_refs=["mock_kb_basic_need_collection"],
            guard_result="pass",
            raw_payload={"adapter": "mock", "model": "mock-omniauto-ai-engine"},
        )


class RealOmniAutoAIEngineAdapter:
    _provider_worker_script = Path(__file__).with_name("ai_provider_worker.py")

    @staticmethod
    def _load_brain():
        settings = get_settings()
        root = Path(settings.c3_omniauto_root).expanduser().resolve()
        app_root = root / "apps" / "wechat_ai_customer_service"
        required = [root, app_root, app_root / "workflows", app_root / "adapters"]
        if not (app_root / "workflows" / "customer_service_brain.py").is_file():
            raise AppError(
                "AI_ENGINE_RUNTIME_MISSING",
                "OmniAuto customer_service_brain 运行代码不存在",
                503,
                {"omniauto_root": str(root), "suggested_action": "mount_omniauto_runtime"},
            )
        for path in reversed(required):
            value = str(path)
            if value not in sys.path:
                sys.path.insert(0, value)
        try:
            module = importlib.import_module(
                "apps.wechat_ai_customer_service.workflows.customer_service_brain"
            )
            return module.maybe_run_customer_service_brain
        except Exception as exc:
            raise AppError(
                "AI_ENGINE_RUNTIME_IMPORT_FAILED",
                "OmniAuto customer_service_brain 加载失败",
                503,
                {"exception_type": type(exc).__name__, "suggested_action": "check_omniauto_runtime"},
            ) from exc

    @staticmethod
    def _load_config() -> dict:
        settings = get_settings()
        if not settings.c3_omniauto_config_path:
            raise AppError(
                "AI_ENGINE_CONFIG_MISSING",
                "缺少 OmniAuto Brain 配置文件",
                503,
                {"required": ["C3_OMNIAUTO_CONFIG_PATH"], "suggested_action": "configure_real_omniauto_brain"},
            )
        path = Path(settings.c3_omniauto_config_path).expanduser().resolve()
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppError(
                "AI_ENGINE_CONFIG_INVALID",
                "OmniAuto Brain 配置文件不可读取或不是合法 JSON",
                503,
                {"path": str(path), "exception_type": type(exc).__name__},
            ) from exc
        if not isinstance(config, dict):
            raise AppError("AI_ENGINE_CONFIG_INVALID", "OmniAuto Brain 配置必须为 JSON 对象", 503)
        brain = dict(config.get("customer_service_brain") or {})
        brain["enabled"] = True
        brain["mode"] = "brain_first"
        brain["fallback_to_legacy_on_error"] = False
        if settings.c3_omniauto_provider:
            brain["provider"] = settings.c3_omniauto_provider
        if settings.c3_omniauto_model:
            brain["model"] = settings.c3_omniauto_model
        if settings.c3_omniauto_base_url:
            brain["base_url"] = settings.c3_omniauto_base_url
        if not str(brain.get("provider") or "").strip() or not str(brain.get("model") or "").strip():
            raise AppError(
                "AI_ENGINE_CONFIG_INVALID",
                "OmniAuto Brain 缺少 provider 或 model",
                503,
                {"required": ["provider", "model"]},
            )
        config["customer_service_brain"] = brain
        return config

    @staticmethod
    def _require_api_key(config: dict) -> None:
        brain = config.get("customer_service_brain") if isinstance(config, dict) else {}
        provider = str((brain or {}).get("provider") or "").strip()
        if str((brain or {}).get("api_key") or "").strip():
            return
        try:
            llm_config = importlib.import_module(
                "apps.wechat_ai_customer_service.llm_config"
            )
            api_key = str(
                llm_config.resolve_llm_api_key(
                    provider=provider,
                    config=brain,
                )
                or ""
            ).strip()
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                "AI_ENGINE_API_KEY_CHECK_FAILED",
                "无法检查 OmniAuto Brain API Key",
                503,
                {
                    "provider": provider,
                    "exception_type": type(exc).__name__,
                    "suggested_action": "check_omniauto_llm_config",
                },
            ) from exc
        if not api_key:
            raise AppError(
                "AI_ENGINE_API_KEY_MISSING",
                "OmniAuto Brain 缺少真实模型 API Key",
                503,
                {
                    "provider": provider,
                    "suggested_action": "configure_real_omniauto_brain_api_key",
                },
            )

    def _run_brain_isolated(
        self,
        *,
        config: dict,
        invocation: dict,
        timeout_seconds: float,
    ) -> dict:
        request = json.dumps(
            {"config": config, "invocation": invocation},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        process = subprocess.Popen(
            [sys.executable, str(self._provider_worker_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, _stderr = process.communicate(input=request, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise AppError(
                "AI_ENGINE_PROVIDER_TIMEOUT",
                "OmniAuto Brain 提供商调用长时间无响应",
                503,
                {"timeout_seconds": timeout_seconds, "suggested_action": "retry_later"},
            ) from exc
        try:
            envelope = json.loads(stdout or "{}")
        except json.JSONDecodeError as exc:
            raise AppError(
                "AI_ENGINE_PROVIDER_FAILED",
                "OmniAuto Brain 隔离进程返回非法结果",
                503,
                {
                    "process_exit_code": process.returncode,
                    "suggested_action": "retry_later",
                },
            ) from exc
        if process.returncode != 0 or envelope.get("ok") is not True:
            error_code = str(envelope.get("error_code") or "AI_ENGINE_PROVIDER_FAILED")
            raise AppError(
                error_code,
                "OmniAuto Brain 调用失败",
                503,
                {
                    "exception_type": str(envelope.get("exception_type") or ""),
                    "process_exit_code": process.returncode,
                    "suggested_action": "retry_later",
                },
            )
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise AppError("AI_ENGINE_CONTRACT_INVALID", "OmniAuto Brain 返回值不是对象", 503)
        return result

    def generate_reply_decision(self, *, conversation_context: dict, message_batch: dict) -> AIEngineDecision:
        self._load_brain()
        config = self._load_config()
        self._require_api_key(config)
        messages = message_batch.get("messages") if isinstance(message_batch.get("messages"), list) else []
        combined = "\n".join(str(item.get("content") or "").strip() for item in messages).strip()
        target_name = str(conversation_context.get("remark_code") or conversation_context.get("conversation_id") or "customer")
        target_state = {
            "conversation_id": conversation_context.get("conversation_id"),
            "customer_profile": conversation_context.get("customer_profile") or {},
            "history": conversation_context.get("history") or [],
            "visual_bridge_inputs": [
                item.get("visual_bridge_input")
                for item in messages
                if item.get("visual_bridge_input") is not None
            ],
            "trigger_type": message_batch.get("trigger_type") or "customer_message",
            "recall_cycle_id": message_batch.get("recall_cycle_id"),
        }
        invocation = {
            "target_name": target_name,
            "target_state": target_state,
            "batch": messages,
            "combined": combined,
            "decision": {},
            "reply_text": "",
            "intent_assist": {},
            "rag_reply": {},
            "llm_reply": {},
            "product_knowledge": conversation_context.get("product_knowledge") or {},
            "data_capture": {},
            "raw_capture": {"messages": messages, "conversation": conversation_context},
            "customer_profile": conversation_context.get("customer_profile") or {},
        }
        timeout_seconds = max(1.0, float(get_settings().c3_brain_provider_timeout_seconds))
        result = self._run_brain_isolated(
            config=config,
            invocation=invocation,
            timeout_seconds=timeout_seconds,
        )

        plan = result.get("brain_plan") if isinstance(result.get("brain_plan"), dict) else {}
        recommended_action = str(plan.get("recommended_action") or "").strip().lower()
        rule_name = str(result.get("rule_name") or "").strip()
        guard_verdict = str(result.get("guard_verdict") or "").strip().lower()
        evidence_refs = list(plan.get("evidence_refs") or [])
        risk_flags = list(plan.get("risk_flags") or [])
        confidence = plan.get("confidence")
        raw_payload = {"omniauto_brain_result": result}

        hard_opt_out = result.get("hard_opt_out") if isinstance(result.get("hard_opt_out"), dict) else {}
        if (
            rule_name == "customer_service_brain_hard_opt_out"
            or recommended_action == "hard_opt_out"
        ):
            if (
                not result.get("adoptable")
                or hard_opt_out.get("detected") is not True
                or not str(hard_opt_out.get("message_event_id") or "").strip()
                or not str(hard_opt_out.get("customer_text") or "").strip()
            ):
                return AIEngineDecision(
                    decision="retry_later",
                    guard_result="failed",
                    error_code="AI_ENGINE_HARD_OPT_OUT_EVIDENCE_INVALID",
                    suggested_action="retry_later",
                    raw_payload=raw_payload,
                )
            return AIEngineDecision(
                decision="hard_opt_out",
                confidence=confidence,
                risk_flags=risk_flags,
                evidence_refs=evidence_refs,
                guard_result="pass",
                hard_opt_out_evidence=dict(hard_opt_out),
                raw_payload=raw_payload,
            )

        if rule_name == "customer_service_brain_reply" and recommended_action == "send_reply":
            reply_text = str(result.get("reply_text") or "").strip()
            if not result.get("adoptable") or not reply_text or result.get("visible_reply_source") != "brain_plan.reply_segments":
                return AIEngineDecision(
                    decision="retry_later",
                    guard_result="failed",
                    error_code="AI_ENGINE_NO_VISIBLE_REPLY",
                    suggested_action="retry_later",
                    raw_payload=raw_payload,
                )
            return AIEngineDecision(
                decision="send_reply",
                reply_text=reply_text,
                confidence=confidence,
                risk_flags=risk_flags,
                evidence_refs=evidence_refs,
                guard_result="pass" if guard_verdict in {"", "pass", "allow", "safe"} else guard_verdict,
                raw_payload=raw_payload,
            )
        if rule_name == "customer_service_brain_handoff" or recommended_action in {"handoff", "handoff_for_approval"}:
            return AIEngineDecision(
                decision=recommended_action if recommended_action in {"handoff", "handoff_for_approval"} else "handoff",
                confidence=confidence,
                handoff_reason_code=str(result.get("reason") or "HANDOFF_REQUIRED")[:64],
                risk_flags=risk_flags,
                evidence_refs=evidence_refs,
                guard_result="handoff",
                raw_payload=raw_payload,
            )
        if (
            rule_name == "customer_service_brain_no_action"
            or recommended_action == "no_action"
        ):
            return AIEngineDecision(
                decision="no_action",
                confidence=confidence,
                risk_flags=risk_flags,
                evidence_refs=evidence_refs,
                guard_result="pass",
                error_code=str(result.get("error_code") or "")[:64] or None,
                suggested_action="no_action",
                raw_payload=raw_payload,
            )
        if (
            rule_name == "customer_service_brain_pause"
            or recommended_action == "pause"
        ):
            return AIEngineDecision(
                decision="pause",
                confidence=confidence,
                handoff_reason_code=str(
                    result.get("reason")
                    or result.get("error_code")
                    or "AI_ENGINE_PAUSED_FOR_MANUAL_REVIEW"
                )[:64],
                risk_flags=risk_flags,
                evidence_refs=evidence_refs,
                guard_result="pause",
                error_code=str(
                    result.get("error_code")
                    or "AI_ENGINE_PAUSED_FOR_MANUAL_REVIEW"
                )[:64],
                suggested_action="sales_handoff",
                raw_payload=raw_payload,
            )
        if (
            rule_name == "customer_service_brain_retry_later"
            or recommended_action == "retry_later"
        ):
            return AIEngineDecision(
                decision="retry_later",
                confidence=confidence,
                risk_flags=risk_flags,
                evidence_refs=evidence_refs,
                guard_result="failed",
                error_code=str(
                    result.get("error_code") or "AI_ENGINE_RETRY_LATER"
                )[:64],
                suggested_action="retry_later",
                raw_payload=raw_payload,
            )
        no_visible_reply = result.get("no_visible_reply") if isinstance(result.get("no_visible_reply"), dict) else {}
        if rule_name == "customer_service_brain_no_visible_reply" or no_visible_reply:
            no_visible_class = str(no_visible_reply.get("class") or result.get("no_visible_reply_class") or "").strip().lower()
            return AIEngineDecision(
                decision="retry_later",
                confidence=confidence,
                risk_flags=risk_flags,
                evidence_refs=evidence_refs,
                guard_result="failed",
                error_code=(
                    "AI_ENGINE_PROVIDER_TIMEOUT"
                    if no_visible_class == "llm_timeout"
                    else (
                        "AI_ENGINE_PROVIDER_FAILED"
                        if no_visible_class == "llm_unavailable"
                        else "AI_ENGINE_NO_VISIBLE_REPLY"
                    )
                ),
                suggested_action="retry_later",
                raw_payload=raw_payload,
            )
        return AIEngineDecision(
            decision="retry_later",
            confidence=confidence,
            risk_flags=risk_flags,
            evidence_refs=evidence_refs,
            guard_result="failed",
            error_code="AI_ENGINE_NO_VISIBLE_REPLY" if recommended_action == "fallback_existing" else "AI_ENGINE_CONTRACT_INVALID",
            suggested_action="retry_later",
            raw_payload=raw_payload,
        )


def get_ai_engine_adapter() -> OmniAutoAIEngineAdapter:
    mode = getattr(get_settings(), "c3_ai_adapter_mode", "real")
    if mode == "mock":
        return MockOmniAutoAIEngineAdapter()
    if mode == "real":
        return RealOmniAutoAIEngineAdapter()
    raise AppError("AI_ENGINE_UNAVAILABLE", "未知 AI Adapter 模式", 503, {"mode": mode, "suggested_action": "check_config"})


def check_ai_engine_readiness() -> dict:
    settings = get_settings()
    mode = str(settings.c3_ai_adapter_mode or "").strip().lower()
    if mode == "mock":
        if settings.is_production:
            raise AppError(
                "AI_ENGINE_MOCK_FORBIDDEN",
                "生产环境禁止使用 Mock Brain",
                503,
                {"suggested_action": "set_c3_ai_adapter_mode_real"},
            )
        return {"ready": True, "mode": "mock", "runtime": "development_only"}
    if mode != "real":
        raise AppError(
            "AI_ENGINE_UNAVAILABLE",
            "未知 AI Adapter 模式",
            503,
            {"mode": mode, "suggested_action": "check_config"},
        )
    adapter = RealOmniAutoAIEngineAdapter()
    adapter._load_brain()
    config = adapter._load_config()
    adapter._require_api_key(config)
    brain = config.get("customer_service_brain") or {}
    return {
        "ready": True,
        "mode": "real",
        "api_key_configured": True,
        "provider": brain.get("provider"),
        "model": brain.get("model"),
    }
