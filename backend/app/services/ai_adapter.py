from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
import sys
import tempfile
from typing import Protocol
import uuid

from app.core.config import get_settings
from app.errors import AppError


_PROVIDER_PROGRESS_STRING_FIELDS = {
    "progress_id",
    "stage",
    "route",
    "event",
    "provider",
    "model",
    "call_id",
    "result_class",
    "provider_request_id",
}


def _safe_progress_value(value: object) -> str:
    text = str(value or "").strip()[:128]
    return "".join(
        character
        if character.isalnum() or character in {"_", "-", "."}
        else "_"
        for character in text
    ).strip("_")


def _read_provider_progress(path: Path, *, progress_id: str) -> list[dict]:
    try:
        if not path.is_file() or path.stat().st_size > 256 * 1024:
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in lines[-96:]:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict) or str(raw.get("progress_id") or "") != progress_id:
            continue
        try:
            schema_version = int(raw.get("schema_version") or 0)
        except (TypeError, ValueError):
            continue
        if schema_version != 1:
            continue
        event: dict = {"schema_version": 1}
        for key in _PROVIDER_PROGRESS_STRING_FIELDS:
            if key not in raw:
                continue
            value = _safe_progress_value(raw[key])
            if value:
                event[key] = value
        for key in (
            "occurred_at_unix_ms",
            "elapsed_ms",
            "status",
        ):
            if key not in raw:
                continue
            try:
                event[key] = max(0, int(raw[key]))
            except (TypeError, ValueError):
                continue
        if "timeout_seconds" in raw:
            try:
                event["timeout_seconds"] = max(
                    1.0,
                    float(raw["timeout_seconds"]),
                )
            except (TypeError, ValueError):
                pass
        if not all(event.get(key) for key in ("progress_id", "stage", "route", "event")):
            continue
        events.append(event)
    return events


def _kill_provider_process(process: subprocess.Popen) -> None:
    """Terminate the isolated Provider worker without inheriting a pipe hang."""

    if os.name == "nt":
        # ``Popen.kill`` can terminate the Python launcher while a descendant
        # still owns the inherited stdout/stderr handles on Windows.  Killing
        # the process tree closes those handles and keeps the hard timeout
        # bounded.  Pass only the system root to the utility so application
        # environment values are not propagated to another process.
        system_root = os.environ.get("SystemRoot", r"C:\\Windows")
        taskkill = str(Path(system_root) / "System32" / "taskkill.exe")
        try:
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=0.25,
                env={"SystemRoot": system_root},
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _schedule_provider_temp_cleanup(temp_dir: Path, process: subprocess.Popen) -> None:
    """Retry cleanup after a timed-out Windows worker releases inherited handles."""

    def cleanup() -> None:
        try:
            process.wait(timeout=0.5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        # A descendant can outlive the launcher briefly on Windows.  Keep the
        # request path bounded and retry cleanup in a daemon thread instead of
        # waiting for that descendant during TemporaryDirectory.__exit__.
        for _ in range(120):
            try:
                shutil.rmtree(temp_dir, ignore_errors=False)
            except OSError:
                time.sleep(0.25)
                continue
            return

    threading.Thread(
        target=cleanup,
        name="chejin-provider-temp-cleanup",
        daemon=True,
    ).start()


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


HIGH_INTENT_REASON_CODE = "CUSTOMER_HIGH_INTENT"
HIGH_INTENT_MARKERS = {
    "customer_high_intent",
    "high_intent",
    "high_purchase_intent",
    "used_car_high_intent",
}

NO_VISIBLE_REPLY_RECOVERY_INSTRUCTION = (
    "上一次 Brain 尝试没有形成可发送的客户可见回复。请基于同一批消息和同一权威证据重新生成完整 BrainPlan；"
    "如果客户是低风险购车咨询，但当前没有可依据的 product_master 车型资料，不得编造车型、价格或库存，"
    "应使用 ask_clarifying_question 或 collect_customer_info，自然追问 1到2 个能继续筛选的需求，"
    "并且必须将完整客户可见文字放入 reply_segments。只有存在真实硬风险时才能转人工。"
)


def _is_structured_high_intent_handoff(
    result: dict,
    plan: dict,
    risk_flags: list[str],
) -> bool:
    """Recognize high intent only from explicit Brain contract evidence.

    Broad business keywords such as price, finance, contract or vehicle
    condition are intentionally not treated as high intent here.
    """

    risk = plan.get("risk") if isinstance(plan.get("risk"), dict) else {}
    intent_assist = (
        result.get("intent_assist")
        if isinstance(result.get("intent_assist"), dict)
        else {}
    )
    markers = {
        str(value or "").strip().lower()
        for value in (
            result.get("reason"),
            plan.get("reason"),
            plan.get("intent"),
            risk.get("handoff_reason"),
            intent_assist.get("intent"),
            intent_assist.get("reason"),
            *risk.get("risk_tags", []),
            *risk_flags,
        )
        if str(value or "").strip()
    }
    return bool(markers & HIGH_INTENT_MARKERS)


def _brain_retry_instruction(message_batch: dict) -> str:
    """Return a focused second-attempt instruction without inventing content.

    Durable C3 retries used to submit the exact same Brain request after an
    invisible-result failure.  The second attempt now receives only the prior
    failure class plus a narrow recovery policy; customer-visible text remains
    authored by Brain and still passes the existing validation and Guard.
    """

    try:
        attempt = int(message_batch.get("generation_attempt") or 0)
    except (TypeError, ValueError):
        attempt = 0
    if attempt <= 1:
        return ""
    previous = (
        message_batch.get("previous_ai_response_snapshot")
        if isinstance(message_batch.get("previous_ai_response_snapshot"), dict)
        else {}
    )
    error_code = str(previous.get("error_code") or "").strip()
    if error_code != "AI_ENGINE_NO_VISIBLE_REPLY":
        return ""
    return NO_VISIBLE_REPLY_RECOVERY_INSTRUCTION


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
    def _load_context_bridge():
        settings = get_settings()
        default_root = Path(__file__).resolve().parents[3] / "worker-client" / "omniauto-rpa"
        root = Path(
            getattr(settings, "c3_omniauto_root", "") or default_root
        ).expanduser().resolve()
        app_root = root / "apps" / "wechat_ai_customer_service"
        for path in reversed(
            [root, app_root, app_root / "workflows", app_root / "adapters"]
        ):
            value = str(path)
            if value not in sys.path:
                sys.path.insert(0, value)
        try:
            module = importlib.import_module(
                "apps.wechat_ai_customer_service.workflows.chejin_brain_context_bridge"
            )
            return module.build_chejin_brain_context
        except Exception as exc:
            raise AppError(
                "AI_ENGINE_RUNTIME_IMPORT_FAILED",
                "OmniAuto 车金上下文桥加载失败",
                503,
                {
                    "exception_type": type(exc).__name__,
                    "suggested_action": "check_omniauto_context_bridge",
                },
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
        provider = str(brain.get("provider") or "").strip().lower()
        model = str(brain.get("model") or "").strip().lower()
        base_url = str(brain.get("base_url") or "").strip().rstrip("/").lower()
        if (
            provider != "deepseek"
            or not model.startswith("deepseek-")
            or base_url not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}
        ):
            raise AppError(
                "AI_ENGINE_PROVIDER_FORBIDDEN",
                "车金正式 Brain 只允许使用 DeepSeek",
                503,
                {
                    "required_provider": "deepseek",
                    "required_base_url": "https://api.deepseek.com",
                    "suggested_action": "restore_formal_deepseek_route",
                },
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
        progress_id = uuid.uuid4().hex
        temp_dir = Path(tempfile.mkdtemp(prefix="chejin-ai-progress-"))
        timeout_cleanup_target: tuple[Path, subprocess.Popen] | None = None
        try:
            progress_path = temp_dir / "provider-progress.ndjson"
            progress_path.touch(mode=0o600)
            stdout_path = temp_dir / "provider.stdout"
            stderr_path = temp_dir / "provider.stderr"
            child_env = os.environ.copy()
            child_env["CHEJIN_AI_PROGRESS_PATH"] = str(progress_path)
            child_env["CHEJIN_AI_PROGRESS_ID"] = progress_id
            stdout_handle = stdout_path.open("wb")
            stderr_handle = stderr_path.open("wb")
            process: subprocess.Popen | None = None
            try:
                process = subprocess.Popen(
                    [sys.executable, str(self._provider_worker_script)],
                    stdin=subprocess.PIPE,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=child_env,
                )
                try:
                    process.communicate(
                        input=request,
                        timeout=timeout_seconds,
                    )
                except subprocess.TimeoutExpired as exc:
                    _kill_provider_process(process)
                    # Keep child output in ordinary files rather than PIPEs.
                    # Windows can otherwise retain an inherited pipe handle
                    # after the worker is killed and make communicate() wait
                    # until a sleeping child exits.
                    try:
                        process.wait(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        pass
                    progress = _read_provider_progress(
                        progress_path,
                        progress_id=progress_id,
                    )
                    timeout_cleanup_target = (temp_dir, process)
                    raise AppError(
                        "AI_ENGINE_PROVIDER_TIMEOUT",
                        "OmniAuto Brain 提供商调用长时间无响应",
                        503,
                        {
                            "timeout_seconds": timeout_seconds,
                            "suggested_action": "retry_later",
                            "provider_progress_id": progress_id,
                            "provider_progress": progress,
                            "last_provider_progress": progress[-1] if progress else None,
                        },
                    ) from exc
            finally:
                stdout_handle.close()
                stderr_handle.close()
                if timeout_cleanup_target is not None:
                    _schedule_provider_temp_cleanup(*timeout_cleanup_target)
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
            progress = _read_provider_progress(
                progress_path,
                progress_id=progress_id,
            )
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
                        "provider_progress_id": progress_id,
                        "provider_progress": progress,
                        "last_provider_progress": progress[-1] if progress else None,
                    },
                ) from exc
            if process.returncode != 0 or envelope.get("ok") is not True:
                error_code = str(
                    envelope.get("error_code") or "AI_ENGINE_PROVIDER_FAILED"
                )
                raise AppError(
                    error_code,
                    "OmniAuto Brain 调用失败",
                    503,
                    {
                        "exception_type": str(
                            envelope.get("exception_type") or ""
                        ),
                        "process_exit_code": process.returncode,
                        "suggested_action": "retry_later",
                        "provider_progress_id": progress_id,
                        "provider_progress": progress,
                        "last_provider_progress": progress[-1] if progress else None,
                    },
                )
            result = envelope.get("result")
            if not isinstance(result, dict):
                raise AppError(
                    "AI_ENGINE_CONTRACT_INVALID",
                    "OmniAuto Brain 返回值不是对象",
                    503,
                    {
                        "provider_progress_id": progress_id,
                        "provider_progress": progress,
                        "last_provider_progress": progress[-1] if progress else None,
                    },
                )
            result["provider_progress_id"] = progress_id
            result["provider_progress"] = progress
            return result
        finally:
            # If setup or provider parsing fails, remove the ordinary
            # temporary directory.  Timed-out workers are cleaned up by the
            # daemon retry scheduled after their handles are closed.
            if timeout_cleanup_target is None:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def generate_reply_decision(self, *, conversation_context: dict, message_batch: dict) -> AIEngineDecision:
        bridge = self._load_context_bridge()
        messages = message_batch.get("messages") if isinstance(message_batch.get("messages"), list) else []
        try:
            bridged_context = bridge(
                brain_context_snapshot=conversation_context.get(
                    "brain_context_snapshot"
                ),
                current_batch=messages,
                expected_conversation_id=str(
                    conversation_context.get("conversation_id") or ""
                ),
            )
        except Exception as exc:
            raise AppError(
                "AI_CONTEXT_BUILD_FAILED",
                "后端冻结的 Brain 历史上下文无法通过唯一上下文桥",
                409,
                {
                    "exception_type": type(exc).__name__,
                    "reason": str(exc)[:128],
                    "suggested_action": "repair_context_bridge_then_retry_same_batch",
                },
            ) from exc
        # Context validity is a deterministic precondition.  Do not load the
        # Brain runtime, configuration, credentials, or Provider path until
        # the frozen MessageEvent snapshot has passed the sole bridge.
        self._load_brain()
        config = self._load_config()
        self._require_api_key(config)
        combined = "\n".join(str(item.get("content") or "").strip() for item in messages).strip()
        target_name = str(conversation_context.get("remark_code") or conversation_context.get("conversation_id") or "customer")
        target_state = {
            "conversation_id": conversation_context.get("conversation_id"),
            # This marker is independent of the projected payload on purpose:
            # if a later refactor drops ``chejin_brain_context``, both normal
            # and fast Brain paths must fail closed instead of reading
            # RawMessageStore or silently using an empty history.
            "history_authority": "chejin_message_events_v1",
            "chejin_context_required": True,
            "customer_profile": conversation_context.get("customer_profile") or {},
            "conversation_context": dict(
                bridged_context.get("conversation_context") or {}
            ),
            "conversation_strategy_state": dict(
                bridged_context.get("conversation_strategy_state") or {}
            ),
            "conversation_interaction_state": dict(
                bridged_context.get("conversation_interaction_state") or {}
            ),
            "chejin_brain_context": bridged_context,
            "chejin_knowledge_release": conversation_context.get(
                "knowledge_release_snapshot"
            )
            or {},
            "chejin_knowledge_required": True,
            "visual_bridge_inputs": [
                item.get("visual_bridge_input")
                for item in messages
                if item.get("visual_bridge_input") is not None
            ],
            "trigger_type": message_batch.get("trigger_type") or "customer_message",
            "recall_cycle_id": message_batch.get("recall_cycle_id"),
        }
        retry_instruction = _brain_retry_instruction(message_batch)
        if retry_instruction:
            target_state["brain_retry_instruction"] = retry_instruction
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

        high_intent = _is_structured_high_intent_handoff(
            result,
            plan,
            risk_flags,
        )
        if high_intent:
            return AIEngineDecision(
                decision="handoff",
                confidence=confidence,
                handoff_reason_code=HIGH_INTENT_REASON_CODE,
                risk_flags=list(
                    dict.fromkeys([*risk_flags, "customer_high_intent"])
                ),
                evidence_refs=evidence_refs,
                guard_result="handoff",
                error_code=HIGH_INTENT_REASON_CODE,
                suggested_action="handoff",
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
            reply_text = str(result.get("reply_text") or "").strip()
            brain_owned_boundary = bool(
                result.get("adoptable")
                and reply_text
                and result.get("visible_reply_source") == "brain_plan.reply_segments"
            )
            return AIEngineDecision(
                decision="reply_then_handoff" if brain_owned_boundary else (
                    recommended_action
                    if recommended_action in {"handoff", "handoff_for_approval"}
                    else "handoff"
                ),
                reply_text=reply_text if brain_owned_boundary else None,
                confidence=confidence,
                handoff_reason_code=str(
                    result.get("reason") or "HANDOFF_REQUIRED"
                )[:64],
                risk_flags=risk_flags,
                evidence_refs=evidence_refs,
                guard_result="handoff",
                error_code=None,
                suggested_action=(
                    "reply_then_handoff"
                    if brain_owned_boundary
                    else "handoff"
                ),
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
