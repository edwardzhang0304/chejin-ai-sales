"""Config-driven guarded WeChat customer-service workflow.

This workflow is the next layer above the single-target minimal loop. It can
poll multiple whitelisted conversations, aggregate recent unprocessed messages,
apply deterministic rules, enforce simple rate limits, optionally send, verify
by reading back, and append audit events.

Default behavior is safe:
- one pass only, unless configured or overridden;
- dry-run unless ``--send`` is passed;
- target conversations must be explicitly enabled in the config;
- fallback replies are blocked unless allowed in the config or CLI.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
ADAPTERS_ROOT = APP_ROOT / "adapters"
WORKFLOWS_ROOT = Path(__file__).resolve().parent
for path in (PROJECT_ROOT, WORKFLOWS_ROOT, APP_ROOT, ADAPTERS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from customer_data_capture import append_customer_row, extract_customer_data
from customer_intent_assist import (
    analyze_intent,
    build_llm_prompt_pack,
    call_deepseek_advisory,
    validate_llm_candidate,
)
from customer_service_loop import BOT_PREFIX, ReplyDecision, decide_reply, format_reply, load_rules
from apps.wechat_ai_customer_service.cloud_gate import cloud_gate_status, cloud_required_enabled
from apps.wechat_ai_customer_service.llm_config import (
    configured_llm_provider,
    resolve_effective_llm_provider,
    resolve_llm_base_url,
    resolve_llm_tier_model,
)
from apps.wechat_ai_customer_service.platform_safety_rules import guard_term_set, load_platform_safety_rules
from apps.wechat_ai_customer_service.wechat_message_envelope import (
    apply_message_envelope_to_record,
    build_message_envelope,
    message_is_visual_or_media_ocr_noise,
)
from apps.wechat_ai_customer_service.message_identity import (
    apply_canonical_identity_fields,
    canonical_input_message_id,
    occurrence_marker_for_message,
)
from apps.wechat_ai_customer_service.wechat_message_normalizer import normalize_wechat_message_record
from final_visible_llm_polish import maybe_polish_customer_visible_reply
from knowledge_loader import build_evidence_pack
from llm_intent_router import route_intent, IntentRouteResult
from llm_reply_synthesis import maybe_synthesize_reply, normalize_visible_reply_surface_noise
from customer_service_brain import (
    build_brain_no_visible_reply_payload,
    effective_brain_settings,
    low_authority_fast_profile_decision,
    maybe_run_customer_service_brain,
)
from customer_service_conversation_strategy import (
    strategy_state_public_audit,
    update_conversation_interaction_state_on_capture,
    update_conversation_strategy_state,
)
from product_knowledge import decide_product_knowledge_reply, load_product_knowledge
from rag_answer_layer import maybe_build_rag_reply
from rag_experience_store import record_rag_reply_experience
from reply_evidence_builder import (
    compact_knowledge_pack,
    collect_catalog_product_aliases,
    is_concrete_catalog_alias,
    mark_finance_process_evidence_auto_reply_allowed,
    relax_finance_process_safety,
)
from realtime_reply_router import (
    build_direct_catalog_price_reply,
    build_synthesis_config_for_route,
    choose_reply_variant,
    current_customer_text,
    de_template_reply_text,
    decide_realtime_reply_route,
    extract_visit_time_label,
    initial_token_budget,
    maybe_build_realtime_reply,
    reply_similarity,
    strip_leading_group_speaker_line,
    update_token_budget_from_synthesis,
)
from reply_style_adapter import adapt_reply_style, infer_source_channel
from wechat_connector import FILE_TRANSFER_ASSISTANT, ROOT, WeChatConnector
from apps.wechat_ai_customer_service.customer_service_live_safety import apply_customer_service_live_safety_guard
from apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime import (
    summarize_listener_result,
    write_runtime_status,
)
from admin_backend.services.customer_service_settings import (
    CustomerServiceSettings,
    normalize_customer_service_brain_mode,
)
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_contamination_guard import (
    message_learning_exclusion_reason,
    text_has_model_reply_marker,
    text_has_test_marker,
)
from admin_backend.services.raw_message_store import RawMessageStore
from apps.wechat_ai_customer_service.admin_backend.services.session_monitor import SessionMonitor
from apps.wechat_ai_customer_service.admin_backend.services.customer_profile_store import CustomerProfileStore
from apps.wechat_ai_customer_service.admin_backend.services.work_queue import WorkQueueService
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id


CONFIG_PATH = ROOT / "apps/wechat_ai_customer_service/configs/default.example.json"
MAX_STORED_IDS = 1000
DEFAULT_MAX_BATCH_MESSAGES = 8


class CustomerServiceBrainStartupError(RuntimeError):
    """Raised when live customer-service startup would not run Brain First."""

    def __init__(self, summary: dict[str, Any]) -> None:
        self.summary = summary
        reasons = ", ".join(str(item) for item in summary.get("fail_reasons", []) or []) or "brain_first_guard_failed"
        super().__init__(f"customer service Brain First guard failed: {reasons}")


BRAIN_FAILURE_ALERT_REASONS = {
    "customer_service_brain_exception",
    "customer_service_brain_llm_unavailable",
    "brain_plan_validation_failed",
    "brain_quality_verification_failed",
    "brain_guard_rejected",
}


def parse_positive_int(value: Any, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def write_workflow_phase(phase: str, **payload: Any) -> None:
    path_value = str(os.getenv("WECHAT_LISTENER_PHASE_LOG_PATH") or "").strip()
    if not path_value:
        return
    record = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "phase": str(phase or "").strip(),
        **payload,
    }
    try:
        path = Path(path_value)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


EXPLICIT_HANDOFF_PATTERNS = (
    r"转人工",
    r"人工客服",
    r"真人客服",
    r"线路切换",
    r"请联系顾问",
    r"(销售|顾问|同事|专员).{0,6}(联系|跟进|对接|接管)",
    r"(转给|交给).{0,6}(同事|顾问|专员|客服)",
)
PRICE_HARD_BOUNDARY_TERMS = ("价格", "报价", "最低", "底价", "优惠", "折扣", "少点", "便宜", "贷款", "金融", "包过", "首付", "月供")
PRICE_ONLY_HARD_BOUNDARY_TERMS = ("价格", "报价", "最低", "底价", "优惠", "折扣", "少点", "便宜")
FINANCE_HARD_BOUNDARY_TERMS = ("贷款", "金融", "包过", "首付", "月供", "征信", "资方", "审批", "利率")
CONDITION_BOUNDARY_TERMS = ("检测报告", "检测", "车况", "事故", "水泡", "火烧", "出险", "维保", "保养记录", "报告")
APPOINTMENT_TERMS = ("试驾", "到店", "看车", "订金", "定金", "留车", "锁车", "预约", "周末", "周六", "周日", "上午", "下午", "几点", "安排", "过去", "来店", "门店")
SAME_DAY_DELIVERY_TERMS = ("办手续", "直接办", "当天办", "提车", "直接提", "当天提", "临牌", "过户", "交车")
SAME_DAY_DELIVERY_STRONG_TERMS = ("办手续", "直接办", "当天办", "当天", "提车", "直接提", "当天提", "临牌", "交车", "办完")
LOCATION_CONTACT_TERMS = ("门店地址", "店地址", "地址", "导航", "位置", "在哪", "哪里", "找谁", "联系人", "对接人", "到了找")
LOCATION_CONTACT_STRONG_TERMS = ("门店地址", "店地址", "地址", "导航", "位置", "在哪", "哪里", "找谁", "对接人", "到了找", "到店找", "跑错")
LOCATION_VISIT_CONTEXT_TERMS = ("门店地址", "店地址", "门店", "店里", "到店", "到了", "过去", "看车", "试驾", "来店", "导航")
CONTACT_DATA_TERMS = ("电话", "手机号", "联系方式", "我叫", "联系人", "姓名", "先生", "女士")
AFTER_SALES_TERMS = ("赔偿", "退款", "纠纷", "投诉", "事故", "水泡", "火烧", "过户", "上牌")
TRADE_IN_TERMS = ("置换", "抵车款", "抵多少", "抵扣", "抵一点", "卖车", "收车", "旧车", "估价", "估个", "估一下", "估一", "怎么估")
INTERNAL_PROBE_TERMS = ("系统提示词", "内部规则", "API密钥", "api密钥", "api key", "密钥", "prompt", "是不是ai", "是不是AI", "是不是机器人", "机器人")
NEW_ENERGY_TERMS = ("新能源", "电池", "三电", "续航", "充电", "混动", "DM-i", "dmi", "插混")
DOCUMENT_TERMS = ("合同", "发票", "开票", "抬头", "税号", "少开", "低开", "保险")
OFF_TOPIC_TERMS = ("外挂", "破解", "脚本", "刷单", "灰产", "游戏外挂")
STANDALONE_GREETING_TERMS = {
    "你好",
    "您好",
    "在吗",
    "有人吗",
    "老板在吗",
    "hello",
    "hi",
    "哈喽",
    "嗨",
}
REPEATABLE_GREETING_PROBE_TERMS = STANDALONE_GREETING_TERMS | {
    "在",
    "在不",
    "在么",
    "在嘛",
    "在呢",
    "再见",
    "拜拜",
    "88",
    "地址",
    "电话",
    "手机",
    "贷款",
    "分期",
    "置换",
    "定位",
    "导航",
    "报价",
    "价格",
    "有吗",
    "能看吗",
    "在店吗",
}
GREETING_BUSINESS_HINT_TERMS = tuple(
    unique
    for group in (
        PRICE_HARD_BOUNDARY_TERMS,
        APPOINTMENT_TERMS,
        SAME_DAY_DELIVERY_TERMS,
        LOCATION_CONTACT_TERMS,
        CONTACT_DATA_TERMS,
        AFTER_SALES_TERMS,
        TRADE_IN_TERMS,
        NEW_ENERGY_TERMS,
        DOCUMENT_TERMS,
        ("车", "预算", "推荐", "公里", "年份", "车况", "事故", "保养", "置换", "贷款"),
    )
    for unique in group
)


@dataclass(frozen=True)
class TargetConfig:
    name: str
    enabled: bool
    exact: bool
    allow_self_for_test: bool
    max_batch_messages: int
    session_key: str = ""


@dataclass(frozen=True)
class BatchSelection:
    batch: list[dict[str, Any]]
    overflow_messages: list[dict[str, Any]]
    eligible_count: int
    max_batch_messages: int

    @property
    def truncated(self) -> bool:
        return bool(self.overflow_messages)


class StateLock:
    """Small cross-process lock for workflow state writes."""

    def __init__(self, path: Path, timeout_seconds: int, stale_seconds: int) -> None:
        self.path = path
        self.timeout_seconds = max(1, timeout_seconds)
        self.stale_seconds = max(60, stale_seconds)
        self.fd: int | None = None

    def __enter__(self) -> "StateLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout_seconds
        while True:
            self.remove_stale_lock()
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                payload = f"pid={os.getpid()}\ncreated_at={datetime.now().isoformat(timespec='seconds')}\n"
                os.write(self.fd, payload.encode("utf-8"))
                return self
            except FileExistsError:
                if time.time() >= deadline:
                    raise TimeoutError(f"Workflow state is locked: {self.path}")
                time.sleep(0.5)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def remove_stale_lock(self) -> None:
        try:
            age_seconds = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if age_seconds >= self.stale_seconds:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        # If lock holder PID is dead, remove immediately regardless of age
        if self._lock_holder_dead():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def _lock_holder_dead(self) -> bool:
        try:
            content = self.path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return True
        for line in content.splitlines():
            if line.startswith("pid="):
                try:
                    pid = int(line.split("=", 1)[1].strip())
                except ValueError:
                    continue
                if pid <= 0:
                    return True
                try:
                    os.kill(pid, 0)
                    return False
                except OSError:
                    return True
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--once", action="store_true", help="Run exactly one iteration.")
    parser.add_argument("--iterations", type=int, help="Override configured iteration count.")
    parser.add_argument("--interval-seconds", type=int, help="Override configured poll interval.")
    parser.add_argument("--send", action="store_true", help="Actually send replies.")
    parser.add_argument(
        "--allow-fallback-send",
        action="store_true",
        help="Allow sending default replies when no rule matched.",
    )
    parser.add_argument(
        "--mark-dry-run",
        action="store_true",
        help="Mark planned dry-run batches as processed.",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Mark existing text messages as processed without replying.",
    )
    parser.add_argument(
        "--write-data",
        action="store_true",
        help="Write extracted customer data to Excel.",
    )
    parser.add_argument(
        "--target",
        action="append",
        help=(
            "Temporary runtime target for bootstrap/dry-run checks. "
            "Send mode is blocked for runtime targets unless they are in config."
        ),
    )
    args = parser.parse_args()

    gate_error = cloud_gate_error_result()
    if gate_error is not None:
        write_runtime_status(
            "stopped",
            str(gate_error.get("message") or "云端授权未通过"),
            cloud_gate=gate_error.get("cloud_gate"),
        )
        print_json(gate_error)
        return 1

    try:
        write_runtime_status("thinking", "正在读取微信消息并调用必要的大模型。")
        write_workflow_phase("workflow_start", config=str(args.config), send=bool(args.send), write_data=bool(args.write_data))
        result = run_workflow(args)
    except CustomerServiceBrainStartupError as exc:
        write_workflow_phase("workflow_brain_startup_guard_failed", summary=exc.summary)
        message = f"微信自动客服启动前安全护栏未通过：{exc}"
        write_runtime_status("stopped", message, brain_startup_guard=exc.summary, last_error=repr(exc))
        result = {"ok": False, "error": str(exc), "brain_startup_guard": exc.summary}
    except Exception as exc:
        write_workflow_phase("workflow_exception", error=repr(exc))
        write_runtime_status("idle", "本轮处理出错，监听会自动重试。", last_error=repr(exc))
        result = {"ok": False, "error": repr(exc)}
    else:
        write_workflow_phase("workflow_done", ok=bool(result.get("ok")), event_count=len(result.get("events") or []))
        status_state, status_message = listener_runtime_status_after_result(result)
        write_runtime_status(status_state, status_message, **summarize_listener_result(result))

    print_json(result)
    return 0 if result.get("ok") else 1


def run_workflow(args: argparse.Namespace) -> dict[str, Any]:
    gate_error = cloud_gate_error_result()
    if gate_error is not None:
        return gate_error

    config = load_config(args.config)
    if bool(args.send) and not bool(args.bootstrap):
        config["_require_customer_service_brain_first_startup_guard"] = True
    config = apply_local_customer_service_settings(config)
    session_routing = config.get("_local_customer_service_session_routing", {}) or {}
    if not isinstance(session_routing, dict):
        session_routing = {}
    respond_all_unread_sessions = bool(session_routing.get("respond_all_unread_sessions", False))
    managed_session_targets = bool(session_routing.get("managed", False))
    ignored_session_names = {
        str(item).strip()
        for item in session_routing.get("ignored_names", []) or []
        if str(item).strip()
    }
    state_path = resolve_path(config.get("state_path"))
    audit_path = resolve_path(config.get("audit_log_path"))
    rules_path = config.get("rules_path")
    rules = load_rules(resolve_path(rules_path)) if rules_path else config

    iterations = resolve_iterations(args, config)
    interval = int(args.interval_seconds or config.get("poll", {}).get("interval_seconds", 15))
    targets = parse_targets(
        config,
        allow_empty=bool(respond_all_unread_sessions or managed_session_targets),
    )
    if args.target:
        if args.send:
            configured_names = {target.name for target in targets}
            runtime_names = {str(name).strip() for name in args.target if str(name).strip()}
            non_configured = sorted(runtime_names - configured_names)
            if non_configured:
                raise ValueError(
                    "Runtime --target is only allowed for bootstrap/dry-run. "
                    "Add runtime targets to config before --send."
                )
        targets = parse_runtime_targets(args.target, config_targets=targets)
    lock_settings = config.get("state_lock", {}) or {}
    with StateLock(
        state_path.with_suffix(state_path.suffix + ".lock"),
        timeout_seconds=int(lock_settings.get("timeout_seconds", 120)),
        stale_seconds=int(lock_settings.get("stale_seconds", 900)),
    ):
        connector = WeChatConnector()
        state = load_state(state_path)

        skip_pre_status = listener_skip_pre_status_check()
        if skip_pre_status:
            status = {
                "ok": True,
                "online": True,
                "adapter": "win32_ocr",
                "state": "pre_status_check_skipped",
                "reason": "managed_listener_low_risk_mode",
            }
        else:
            status = connector.require_online()
        summary: dict[str, Any] = {
            "ok": True,
            "dry_run": not args.send,
            "iterations": iterations,
            "status": status,
            "targets": [target.name for target in targets],
            "events": [],
        }

        multi_target_cfg = config.get("multi_target") or {}
        use_multi_target = bool(multi_target_cfg.get("enabled")) and not args.target
        session_monitor: SessionMonitor | None = None
        if use_multi_target:
            whitelist = set() if respond_all_unread_sessions else {t.name for t in targets}
            session_monitor = SessionMonitor(
                whitelist=whitelist,
                blacklist=ignored_session_names,
                max_targets_per_iteration=int(multi_target_cfg.get("max_targets_per_iteration", 5)),
                min_switch_interval_seconds=int(multi_target_cfg.get("min_switch_interval_seconds", 2)),
                initial_preview_can_raise_unread=bool(multi_target_cfg.get("initial_preview_can_raise_unread", True)),
                preview_change_can_raise_unread=bool(multi_target_cfg.get("preview_change_can_raise_unread", True)),
                short_preview_can_raise_unread=bool(multi_target_cfg.get("short_preview_can_raise_unread", True)),
                require_unread_badge_for_dispatch=bool(multi_target_cfg.get("require_unread_badge_for_dispatch", False)),
                require_preview_signal_with_unread_badge=bool(multi_target_cfg.get("require_preview_signal_with_unread_badge", False)),
            )
        previous_active_names: set[str] = set()

        for iteration in range(iterations):
            iteration_events = []
            if use_multi_target and session_monitor is not None:
                active = session_monitor.poll(connector)
                active_names = {str(item.name) for item in active if str(getattr(item, "name", "") or "")}
                if active and active_names != previous_active_names:
                    warmup_delay = multi_target_change_warmup_delay_seconds(multi_target_cfg)
                    if warmup_delay > 0:
                        write_workflow_phase(
                            "session_change_warmup_start",
                            iteration=iteration + 1,
                            active_targets=sorted(active_names),
                            delay_seconds=round(warmup_delay, 3),
                        )
                        time.sleep(warmup_delay)
                        refreshed_active = session_monitor.poll(connector)
                        active = coalesce_active_targets(active, refreshed_active)
                        active_names = {str(item.name) for item in active if str(getattr(item, "name", "") or "")}
                        write_workflow_phase(
                            "session_change_warmup_done",
                            iteration=iteration + 1,
                            active_targets=sorted(active_names),
                        )
                previous_active_names = active_names
                dynamic_targets = build_iteration_targets(
                    config_targets=targets,
                    active_targets=active,
                    multi_target_cfg=multi_target_cfg,
                    allow_dynamic_active_targets=respond_all_unread_sessions,
                    blocked_names=ignored_session_names,
                )
                summary["active_target_names"] = [str(item.name) for item in active]
                summary["iteration_target_names"] = [str(item.name) for item in dynamic_targets]
                summary["active_sessions"] = session_monitor.all_sessions()
            else:
                dynamic_targets = targets

            for target in dynamic_targets:
                if args.bootstrap:
                    event = bootstrap_target(connector, target, state, config)
                else:
                    event = process_target(
                        connector=connector,
                        target=target,
                        config=config,
                        rules=rules,
                        state=state,
                        send=bool(args.send),
                        write_data=bool(args.write_data),
                        allow_fallback_send=bool(args.allow_fallback_send),
                        mark_dry_run=bool(args.mark_dry_run),
                    )
                event["iteration"] = iteration + 1
                append_audit(audit_path, event)
                iteration_events.append(event)
                if use_multi_target and session_monitor is not None:
                    session_monitor.reset_unread(target.name)
            save_state(state_path, state)
            summary["events"].extend(iteration_events)
            if any(item.get("ok") is False for item in iteration_events if isinstance(item, dict)):
                summary["ok"] = False
            if iteration < iterations - 1:
                time.sleep(max(1, interval))

    return summary


def cloud_gate_error_result() -> dict[str, Any] | None:
    if not cloud_required_enabled():
        return None
    gate = cloud_gate_status()
    if gate.get("ok"):
        return None
    message = "云端授权未通过，当前自动客服已锁定。请先连接服务端并刷新共享行业知识库。"
    return {
        "ok": False,
        "error": "cloud_authoritative_access_required",
        "message": message,
        "cloud_gate": gate,
    }


def listener_skip_pre_status_check() -> bool:
    raw = os.getenv("WECHAT_LISTENER_SKIP_PRE_STATUS")
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _enqueue_post_reply_work(
    target: TargetConfig,
    config: dict[str, Any],
    raw_capture: dict[str, Any],
    data_capture: dict[str, Any],
) -> None:
    """Enqueue background tasks after a reply has been sent."""
    try:
        tenant_id = active_tenant_id()
        queue = WorkQueueService(tenant_id=tenant_id)

        # 1. experience_interpretation — raw message batch LLM processing
        batch_id = str((raw_capture or {}).get("batch", {}).get("batch_id") or "")
        raw_settings = config.get("raw_messages", {}) or {}
        if batch_id and raw_settings.get("auto_learn", False):
            queue.enqueue(
                kind="experience_interpretation",
                payload={
                    "batch_id": batch_id,
                    "tenant_id": tenant_id,
                    "use_llm": raw_settings.get("use_llm", True) is not False,
                },
                queue="customer_service",
                dedupe_key=f"exp_interp:{tenant_id}:{batch_id}",
                priority=4,
            )

        # 2. customer_data_sync — Excel write or other data persistence
        if data_capture.get("enabled") and data_capture.get("is_customer_data"):
            queue.enqueue(
                kind="customer_data_sync",
                payload={
                    "target_name": target.name,
                    "tenant_id": tenant_id,
                    "data_capture": {
                        "fields": data_capture.get("fields", {}),
                        "message_ids": data_capture.get("message_ids", []),
                        "raw_text": data_capture.get("raw_text", ""),
                    },
                },
                queue="customer_service",
                dedupe_key=f"data_sync:{tenant_id}:{target.name}:{','.join(str(item) for item in data_capture.get('message_ids', []))}",
                priority=3,
            )

        # 3. customer_profile_analysis — async LLM tag extraction
        profile_cfg = config.get("customer_profiles") or {}
        if isinstance(profile_cfg, dict) and profile_cfg.get("analysis", {}).get("enabled", True):
            queue.enqueue(
                kind="customer_profile_analysis",
                payload={
                    "target_name": target.name,
                    "tenant_id": tenant_id,
                },
                queue="customer_service",
                dedupe_key=f"profile_analysis:{tenant_id}:{target.name}",
                priority=6,
            )

        # 4. raw_message_archive — periodic cleanup (deduped, low priority)
        queue.enqueue(
            kind="raw_message_archive",
            payload={"tenant_id": tenant_id, "days": 30},
            queue="customer_service",
            dedupe_key=f"raw_archive:{tenant_id}",
            priority=8,
        )
    except Exception:
        # Work queue failures must never block the reply path
        pass


def process_target(
    connector: WeChatConnector,
    target: TargetConfig,
    config: dict[str, Any],
    rules: dict[str, Any],
    state: dict[str, Any],
    send: bool,
    write_data: bool,
    allow_fallback_send: bool,
    mark_dry_run: bool,
) -> dict[str, Any]:
    target_state_key = str(getattr(target, "session_key", "") or target.name)
    target_state = state.setdefault("targets", {}).setdefault(
        target_state_key,
        {
            "processed_message_ids": [],
            "processed_content_keys": [],
            "handoff_message_ids": [],
            "sent_replies": [],
            "reply_timestamps": [],
        },
    )
    if target_state_key != target.name:
        target_state["_session_key"] = target_state_key
        target_state["_display_name"] = target.name
    write_workflow_phase("target_get_messages_start", target=target.name, send=bool(send))
    payload = connector.get_messages(
        target.name,
        exact=target.exact,
        session_key=str(getattr(target, "session_key", "") or ""),
    )
    write_workflow_phase(
        "target_get_messages_done",
        target=target.name,
        ok=bool(payload.get("ok")),
        message_count=len(payload.get("messages") or []),
    )
    if not payload.get("ok"):
        return base_event(target, "error", {"messages": payload})
    console_settings = config.get("_local_customer_service_settings", {}) or {}
    if console_settings.get("enabled") is False:
        raw_capture = maybe_record_raw_messages(target, config, payload.get("messages", []) or [])
        return base_event(target, "skipped", {"reason": "customer_service_disabled", "raw_capture": raw_capture})
    if str(console_settings.get("reply_mode") or "") == "record_only":
        raw_capture = maybe_record_raw_messages(target, config, payload.get("messages", []) or [])
        return base_event(target, "skipped", {"reason": "record_only_mode", "raw_capture": raw_capture})
    if str(console_settings.get("reply_mode") or "") == "manual_assist":
        send = False

    write_workflow_phase("history_backfill_start", target=target.name, message_count=len(payload.get("messages") or []))
    payload = maybe_enrich_messages_with_history(
        connector=connector,
        target=target,
        config=config,
        payload=payload,
        target_state=target_state,
    )
    write_workflow_phase("history_backfill_done", target=target.name, message_count=len(payload.get("messages") or []))
    payload = normalize_capture_payload_for_semantic_processing(payload, target=target, config=config)
    raw_capture = maybe_record_raw_messages(target, config, payload.get("messages", []) or [])
    selection = select_batch_details(
        payload.get("messages", []) or [],
        target_state=target_state,
        allow_self_for_test=target.allow_self_for_test,
        max_batch_messages=target.max_batch_messages,
        config=config,
    )
    if selection.eligible_count <= 0:
        scheduler_selection = select_scheduler_authoritative_batch_details(
            payload,
            target_state=target_state,
            allow_self_for_test=target.allow_self_for_test,
            max_batch_messages=target.max_batch_messages,
            config=config,
        )
        if scheduler_selection.eligible_count > 0:
            payload["_scheduler_authoritative_selection_used"] = True
            selection = scheduler_selection
    write_workflow_phase(
        "batch_selection_done",
        target=target.name,
        eligible_count=selection.eligible_count,
        overflow_count=len(selection.overflow_messages),
        scheduler_authoritative_selection_used=bool(payload.get("_scheduler_authoritative_selection_used")),
    )
    history_backfill_meta = payload.get("_history_backfill", {}) if isinstance(payload.get("_history_backfill"), dict) else {}
    if history_gap_risk_blocks_reply(history_backfill_meta, config) and selection.eligible_count > 0:
        write_runtime_status(
            "paused",
            "检测到微信消息窗口可能存在缺口，已暂停自动回复，等待回填确认或人工处理。",
            target=target.name,
            last_action="blocked",
            last_reason="history_backfill_gap_risk",
            history_backfill=history_backfill_meta,
        )
        return base_event(
            target,
            "blocked",
            {
                "reason": "history_backfill_gap_risk",
                "raw_capture": raw_capture,
                "batch_selection": batch_selection_payload(selection),
                "history_backfill": history_backfill_meta,
                "message_count": selection.eligible_count,
                "dry_run": not send,
            },
        )
    batch = selection.batch
    if not batch:
        return base_event(
            target,
            "skipped",
            {
                "reason": "no eligible unprocessed text messages",
                "raw_capture": raw_capture,
                "batch_selection": batch_selection_payload(selection),
            },
        )

    # LLM-only planning runs in parallel, so keep it side-effect-free. The real
    # send/write path still updates customer profile stats below.
    profile_store = CustomerProfileStore()
    if send or write_data:
        profile = profile_store.get_or_create(target_name=target.name, display_name=target.name)
        profile_store.increment_message_stats(target_name=target.name, is_reply=False)
    else:
        profile = profile_store.get_profile(target_name=target.name) or {
            "target_name": target.name,
            "display_name": target.name,
            "basic_info": {},
            "tags": {},
            "conversation_summary": "",
            "greeting_preference": {},
        }

    routing_batch = normalize_batch_content_for_reply_routing(batch)
    semantic_batch_plan = plan_message_batch_semantics(routing_batch, config)
    combined = str(semantic_batch_plan.get("combined_text") or "\n".join(str(item.get("content") or "") for item in batch))
    preference_context_update = update_conversation_preference_context(target_state, combined)
    strategy_state = update_conversation_strategy_state(target_state, combined)
    message_ids = [reply_input_message_identity(item) for item in batch if reply_input_message_identity(item)]
    interaction_state = update_conversation_interaction_state_on_capture(
        target_state,
        combined,
        message_ids=message_ids,
    )
    if send and should_defer_standalone_greeting(config, routing_batch, combined):
        mark_coalesced_messages(
            target_state,
            batch,
            reason="standalone_greeting_deferred_for_rpa_safety",
        )
        mark_coalesced_messages(
            target_state,
            selection.overflow_messages,
            reason="overflow_coalesced_after_standalone_greeting_defer",
        )
        return base_event(
            target,
            "skipped",
            {
                "reason": "standalone_greeting_deferred_for_rpa_safety",
                "message_ids": message_ids,
                "message_count": len(batch),
                "combined_content": combined,
                "raw_capture": raw_capture,
                "batch_selection": batch_selection_payload(selection),
                "semantic_batch_plan": semantic_batch_plan,
                "conversation_strategy_state": strategy_state_public_audit(strategy_state),
                "conversation_interaction_state": interaction_state,
                "reply_routing_normalization": batch_routing_normalization_payload(batch, routing_batch),
                "dry_run": False,
            },
        )
    write_runtime_status(
        "thinking",
        f"正在处理「{target.name}」的 {len(batch)} 条新消息。",
        target=target.name,
        message_count=len(batch),
        message_ids=message_ids,
    )
    write_workflow_phase(
        "conversation_strategy_state_updated",
        target=target.name,
        **strategy_state_public_audit(strategy_state),
    )
    low_authority_fast_precheck = brain_first_low_authority_fast_plan_precheck(
        config=config,
        combined=combined,
        batch=batch,
        target_state=target_state,
    )
    if low_authority_fast_precheck.get("enabled") and not send:
        write_workflow_phase(
            "brain_first_low_authority_fast_precheck",
            target=target.name,
            enabled=True,
            reason=low_authority_fast_precheck.get("reason"),
        )
        decision = ReplyDecision(
            reply_text="",
            rule_name="brain_first_low_authority_fast_precheck",
            matched=True,
            need_handoff=False,
            reason=str(low_authority_fast_precheck.get("reason") or "brain_first_low_authority_fast_precheck"),
        )
        reply_text = ""
        data_capture = {"enabled": False, "reason": "skipped_for_brain_first_low_authority_fast_precheck"}
        product_knowledge = {"enabled": False, "reason": "skipped_for_brain_first_low_authority_fast_precheck"}
        intent_assist = skipped_intent_assist(config, "skipped_for_brain_first_low_authority_fast_precheck")
        event = base_event(
            target,
            "planned",
            {
                "message_ids": message_ids,
                "message_count": len(batch),
                "combined_content": combined,
                "decision": {
                    **decision.__dict__,
                    "raw_reply_text": decision.reply_text,
                    "reply_text": reply_text,
                },
                "data_capture": data_capture,
                "raw_capture": raw_capture,
                "batch_selection": {
                    **batch_selection_payload(selection),
                    "scheduler_authoritative_selection_used": bool(payload.get("_scheduler_authoritative_selection_used")),
                },
                "semantic_batch_plan": semantic_batch_plan,
                "reply_routing_normalization": batch_routing_normalization_payload(batch, routing_batch),
                "history_backfill": payload.get("_history_backfill", {}),
                "product_knowledge": product_knowledge,
                "conversation_context_update": dict(preference_context_update),
                "conversation_context_update_source": "customer_preference_capture" if preference_context_update else "",
                "intent_result": {
                    "intent": "greeting",
                    "confidence": 0.95,
                    "reasoning": "brain_first_low_authority_fast_precheck",
                    "entities": {},
                    "source": "brain_first_low_authority_fast_precheck",
                },
                "intent_assist": intent_assist,
                "dry_run": not send,
                "brain_first_low_authority_fast_precheck": low_authority_fast_precheck,
            },
        )
        write_workflow_phase("customer_service_brain_start", target=target.name)
        try:
            event["customer_service_brain"] = maybe_run_customer_service_brain(
                config=config,
                target_name=target.name,
                target_state=target_state,
                batch=batch,
                combined=combined,
                decision=decision,
                reply_text=reply_text,
                intent_assist=intent_assist,
                rag_reply={},
                llm_reply={},
                product_knowledge=product_knowledge,
                data_capture=data_capture,
                raw_capture=raw_capture,
                customer_profile=profile,
            )
        except Exception as exc:
            brain_settings = effective_brain_settings(config)
            event["customer_service_brain"] = {
                "enabled": bool(brain_settings.get("enabled", False)),
                "mode": str(brain_settings.get("mode") or "off"),
                "applied": False,
                "adoptable": False,
                "reason": "customer_service_brain_exception",
                "error": repr(exc),
            }
            if (
                event["customer_service_brain"]["enabled"]
                and event["customer_service_brain"]["mode"] == "brain_first"
                and brain_settings.get("fallback_to_legacy_on_error", False) is False
            ):
                event["customer_service_brain"] = build_brain_no_visible_reply_payload(
                    event["customer_service_brain"],
                    combined=combined,
                    reason="customer_service_brain_exception",
                )
        write_workflow_phase(
            "customer_service_brain_done",
            target=target.name,
            enabled=bool(event["customer_service_brain"].get("enabled")),
            applied=bool(event["customer_service_brain"].get("applied")),
            adoptable=bool(event["customer_service_brain"].get("adoptable")),
            reason=event["customer_service_brain"].get("reason"),
        )
        event["customer_service_brain_failure_alert"] = maybe_record_customer_service_brain_failure_alert(
            config=config,
            target_state=target_state,
            target=target,
            batch=batch,
            combined=combined,
            brain_result=event["customer_service_brain"],
            product_knowledge=product_knowledge,
        )
        if customer_service_brain_controls_reply(event["customer_service_brain"]):
            event["customer_service_brain_legacy_generators"] = {
                "disabled": True,
                "reason": "brain_first_authoritative_control",
                "disabled_modules": ["rag_response", "realtime_reply", "llm_reply_synthesis"],
            }
            if not should_adopt_customer_service_brain(event["customer_service_brain"]):
                return block_for_customer_service_brain_no_visible_reply(
                    event=event,
                    target=target,
                    target_state=target_state,
                    batch=batch,
                    combined=combined,
                    config=config,
                    brain_result=event["customer_service_brain"],
                    product_knowledge=product_knowledge,
                )
        brain_result = event.get("customer_service_brain") if isinstance(event.get("customer_service_brain"), dict) else {}
        if should_adopt_customer_service_brain(brain_result):
            decision, reply_text = adopt_customer_service_brain_reply_event(
                event=event,
                brain_result=brain_result,
                config=config,
            )
            if decision.need_handoff and not decision.reply_text:
                return block_for_customer_service_brain_no_visible_reply(
                    event=event,
                    target=target,
                    target_state=target_state,
                    batch=batch,
                    combined=combined,
                    config=config,
                    brain_result=brain_result,
                    product_knowledge=product_knowledge,
                )
            clear_no_relevant_handoff_after_safe_brain_reply(event["intent_assist"], brain_result)
            reply_text = normalize_brain_owned_customer_visible_reply_text(reply_text, config=config)
            event["decision"]["reply_text"] = reply_text
            event["reply_text"] = reply_text
            event["reply_style_adapter"] = {
                "applied": False,
                "reason": "skipped_for_customer_service_brain",
                "reply_text": reply_text,
                "source_channel": "brain",
            }
            event["brain_first_reply_audit"] = build_brain_first_reply_audit(event, reply_text=reply_text)
            return event
    if send:
        backoff = get_rate_limit_backoff(target_state, message_ids)
        if backoff:
            return base_event(
                target,
                "skipped",
                {
                    "reason": "rate_limit_backoff_active",
                    "message_ids": message_ids,
                    "message_count": len(batch),
                    "retry_after_at": backoff.get("retry_after_at"),
                    "rate_limit_reason": backoff.get("reason"),
                },
            )

    # 1. Build evidence pack for intent analysis and synthesis
    evidence_pack = build_evidence_pack(
        combined,
        context=build_evidence_context_for_pack(
            config,
            conversation_context_from_product_result(
                target_state.get("conversation_context", {}) or {}
            ),
        ),
    )

    # 2. LLM intent routing — replaces keyword-based customer_data gate
    write_workflow_phase("intent_route_start", target=target.name, message_count=len(batch))
    intent_result = route_intent(
        combined=combined,
        config=config,
        evidence_pack=evidence_pack,
        target_state=target_state,
    )
    write_workflow_phase("intent_route_done", target=target.name, intent=intent_result.intent, confidence=intent_result.confidence)

    # 3. Branch by intent
    data_capture: dict[str, Any] = {"enabled": False}
    product_knowledge: dict[str, Any] | None = None
    decision: ReplyDecision

    if intent_result.intent == "customer_data_provide":
        write_workflow_phase("data_capture_start", target=target.name, intent=intent_result.intent)
        data_capture = maybe_capture_customer_data(
            config=config,
            target_state=target_state,
            target=target,
            batch=batch,
            combined=combined,
            write_data=False,
            intent_result=intent_result,
        )
        write_workflow_phase("data_capture_done", target=target.name, enabled=bool(data_capture.get("enabled")), complete=bool(data_capture.get("complete")))
        if data_capture.get("enabled"):
            data_capture["write_requested"] = write_data
        decision = decide_reply_with_data_capture(
            combined, rules, config, data_capture, product_knowledge
        )

    elif intent_result.intent == "handoff_request":
        decision = ReplyDecision(
            reply_text=handoff_acknowledgement_text(config),
            rule_name="handoff_request",
            matched=True,
            need_handoff=True,
            reason="customer_explicit_handoff_intent",
        )

    else:
        # product_inquiry, general_chat, greeting, unclear
        # Extract data fields without blocking the flow
        write_workflow_phase("data_capture_start", target=target.name, intent=intent_result.intent)
        data_capture = maybe_capture_customer_data(
            config=config,
            target_state=target_state,
            target=target,
            batch=batch,
            combined=combined,
            write_data=False,
            intent_result=intent_result,
        )
        write_workflow_phase("data_capture_done", target=target.name, enabled=bool(data_capture.get("enabled")), complete=bool(data_capture.get("complete")))
        if data_capture.get("enabled"):
            data_capture["write_requested"] = write_data

        write_workflow_phase("product_knowledge_start", target=target.name)
        product_knowledge = maybe_match_product_knowledge(
            config, target_state, combined, data_capture
        )
        write_workflow_phase(
            "product_knowledge_done",
            target=target.name,
            matched=bool(product_knowledge and product_knowledge.get("matched")),
            match_type=(product_knowledge or {}).get("match_type") if product_knowledge else "",
        )
        update_conversation_context(target_state, product_knowledge)
        decision = decide_reply_with_data_capture(
            combined, rules, config, data_capture, product_knowledge
        )
    reply_prefix = configured_reply_prefix(config)
    reply_text = format_reply(decision.reply_text, reply_prefix)
    fallback_allowed = bool(allow_fallback_send or config.get("reply", {}).get("allow_fallback_send"))

    event = base_event(
        target,
        "planned",
        {
            "message_ids": message_ids,
            "message_count": len(batch),
            "combined_content": combined,
            "decision": {
                **decision.__dict__,
                "raw_reply_text": decision.reply_text,
                "reply_text": reply_text,
            },
            "data_capture": data_capture,
            "raw_capture": raw_capture,
            "batch_selection": {
                **batch_selection_payload(selection),
                "scheduler_authoritative_selection_used": bool(payload.get("_scheduler_authoritative_selection_used")),
            },
            "semantic_batch_plan": semantic_batch_plan,
            "reply_routing_normalization": batch_routing_normalization_payload(batch, routing_batch),
            "history_backfill": payload.get("_history_backfill", {}),
            "product_knowledge": product_knowledge,
            "conversation_context_update": dict(preference_context_update),
            "conversation_context_update_source": "customer_preference_capture" if preference_context_update else "",
            "intent_result": intent_result.to_dict(),
            "intent_assist": skipped_intent_assist(config, "not_evaluated_yet"),
            "dry_run": not send,
        },
    )

    rate_check = check_rate_limit(target_state, config)
    if send and not rate_check["allowed"]:
        return handle_rate_limit_block(
            connector=connector,
            target=target,
            config=config,
            target_state=target_state,
            event=event,
            message_ids=message_ids,
            rate_check=rate_check,
        )

    clear_rate_limit_backoff(target_state, message_ids)

    write_workflow_phase("intent_assist_start", target=target.name)
    event["intent_assist"] = maybe_analyze_intent(
        config=config,
        combined=combined,
        decision=decision,
        reply_text=reply_text,
        data_capture=data_capture,
        product_knowledge=product_knowledge,
    )
    write_workflow_phase(
        "intent_assist_done",
        target=target.name,
        needs_handoff=bool(event["intent_assist"].get("needs_handoff")),
        llm_status=((event["intent_assist"].get("llm_advisory") or {}).get("status") if isinstance(event["intent_assist"].get("llm_advisory"), dict) else ""),
    )
    write_workflow_phase("customer_service_brain_start", target=target.name)
    try:
        event["customer_service_brain"] = maybe_run_customer_service_brain(
            config=config,
            target_name=target.name,
            target_state=target_state,
            batch=batch,
            combined=combined,
            decision=decision,
            reply_text=reply_text,
            intent_assist=event["intent_assist"],
            rag_reply={},
            llm_reply={},
            product_knowledge=product_knowledge,
            data_capture=data_capture,
            raw_capture=raw_capture,
            customer_profile=profile,
        )
    except Exception as exc:
        brain_settings = effective_brain_settings(config)
        event["customer_service_brain"] = {
            "enabled": bool(brain_settings.get("enabled", False)),
            "mode": str(brain_settings.get("mode") or "off"),
            "applied": False,
            "adoptable": False,
            "reason": "customer_service_brain_exception",
            "error": repr(exc),
        }
        if (
            event["customer_service_brain"]["enabled"]
            and event["customer_service_brain"]["mode"] == "brain_first"
            and brain_settings.get("fallback_to_legacy_on_error", False) is False
        ):
            event["customer_service_brain"] = build_brain_no_visible_reply_payload(
                event["customer_service_brain"],
                combined=combined,
                reason="customer_service_brain_exception",
            )
    write_workflow_phase(
        "customer_service_brain_done",
        target=target.name,
        enabled=bool(event["customer_service_brain"].get("enabled")),
        applied=bool(event["customer_service_brain"].get("applied")),
        adoptable=bool(event["customer_service_brain"].get("adoptable")),
        reason=event["customer_service_brain"].get("reason"),
    )
    event["customer_service_brain_failure_alert"] = maybe_record_customer_service_brain_failure_alert(
        config=config,
        target_state=target_state,
        target=target,
        batch=batch,
        combined=combined,
        brain_result=event["customer_service_brain"],
        product_knowledge=product_knowledge,
    )
    if customer_service_brain_controls_reply(event["customer_service_brain"]):
        config = config_with_legacy_reply_generators_disabled_for_brain(config)
        event["customer_service_brain_legacy_generators"] = {
            "disabled": True,
            "reason": "brain_first_authoritative_control",
            "disabled_modules": ["rag_response", "realtime_reply", "llm_reply_synthesis"],
        }
        if not should_adopt_customer_service_brain(event["customer_service_brain"]):
            return block_for_customer_service_brain_no_visible_reply(
                event=event,
                target=target,
                target_state=target_state,
                batch=batch,
                combined=combined,
                config=config,
                brain_result=event["customer_service_brain"],
                product_knowledge=product_knowledge,
            )
    write_workflow_phase("rag_reply_start", target=target.name)
    rag_reply = maybe_build_rag_reply(
        config=config,
        text=combined,
        decision=decision,
        reply_text=reply_text,
        intent_assist=event["intent_assist"],
        product_knowledge=product_knowledge,
        data_capture=data_capture,
    )
    write_workflow_phase("rag_reply_done", target=target.name, applied=bool(rag_reply.get("applied")))
    event["rag_reply"] = rag_reply
    if rag_reply.get("applied"):
        decision = ReplyDecision(
            reply_text=str(rag_reply.get("raw_reply_text") or ""),
            rule_name=str(rag_reply.get("rule_name") or "rag_context_reply"),
            matched=True,
            need_handoff=bool(rag_reply.get("needs_handoff")),
            reason=str(rag_reply.get("reason") or "safe_rag_context_reply"),
        )
        reply_text = str(rag_reply.get("reply_text") or reply_text)
        event["decision"] = {
            **decision.__dict__,
            "raw_reply_text": decision.reply_text,
            "reply_text": reply_text,
        }
    if rag_reply.get("applied") and (config.get("rag_response", {}) or {}).get("skip_llm_after_apply", True):
        llm_settings = (config.get("intent_assist", {}) or {}).get("llm_advisory", {}) or {}
        llm_reply = {
            "enabled": bool(llm_settings.get("enabled", False)),
            "apply_to_reply": bool(llm_settings.get("apply_to_reply", False)),
            "applied": False,
            "reason": "skipped_after_rag_reply",
        }
    else:
        write_workflow_phase("llm_reply_apply_start", target=target.name)
        llm_reply = maybe_apply_llm_reply(
            config=config,
            decision=decision,
            reply_text=reply_text,
            intent_assist=event["intent_assist"],
            product_knowledge=product_knowledge,
            data_capture=data_capture,
            combined=combined,
        )
        write_workflow_phase("llm_reply_apply_done", target=target.name, applied=bool(llm_reply.get("applied")), reason=llm_reply.get("reason"))
    event["llm_reply"] = llm_reply
    if llm_reply.get("applied"):
        decision = ReplyDecision(
            reply_text=str(llm_reply.get("raw_reply_text") or ""),
            rule_name=str(llm_reply.get("rule_name") or "llm_boundary_reply"),
            matched=True,
            need_handoff=bool(llm_reply.get("needs_handoff")),
            reason=str(llm_reply.get("reason") or "llm_boundary_reply"),
        )
        reply_text = str(llm_reply.get("reply_text") or reply_text)
        event["decision"] = {
            **decision.__dict__,
            "raw_reply_text": decision.reply_text,
            "reply_text": reply_text,
        }

    recent_reply_texts = recent_customer_visible_reply_texts(target_state)
    realtime_combined = build_realtime_context_combined(combined, target_state)
    event["realtime_context"] = {
        "applied": realtime_combined != combined,
        "text": realtime_combined[:1200] if realtime_combined != combined else "",
    }
    write_workflow_phase("realtime_route_start", target=target.name)
    realtime_evidence_pack = build_realtime_router_evidence_pack(
        combined=realtime_combined,
        evidence_pack=evidence_pack,
        config=config,
    )
    runtime_route = decide_realtime_reply_route(
        config=config,
        combined=realtime_combined,
        decision=decision,
        intent_result=intent_result,
        intent_assist=event["intent_assist"],
        rag_reply=rag_reply,
        llm_reply=llm_reply,
        product_knowledge=product_knowledge,
        data_capture=data_capture,
        evidence_pack=realtime_evidence_pack,
        recent_reply_texts=recent_reply_texts,
    )
    write_workflow_phase(
        "realtime_route_done",
        target=target.name,
        level=runtime_route.get("level"),
        foreground_llm_allowed=bool(runtime_route.get("foreground_llm_allowed")),
        reason=runtime_route.get("reason"),
    )
    event["runtime_route"] = runtime_route
    if runtime_route.get("level") == "L0" and runtime_route.get("reason") == "deterministic_handoff_or_high_risk_boundary":
        if is_low_information_handoff_reply(reply_text, config):
            reply_text = format_reply(
                handoff_acknowledgement_text(config, combined=realtime_combined or combined),
                configured_reply_prefix(config),
            )
        raw_l0_reply = split_reply_prefix(reply_text, config)[1] or decision.reply_text
        decision = ReplyDecision(
            reply_text=raw_l0_reply,
            rule_name=decision.rule_name,
            matched=decision.matched,
            need_handoff=True,
            reason=str(runtime_route.get("reason") or decision.reason),
        )
        event["decision"] = {
            **decision.__dict__,
            "raw_reply_text": decision.reply_text,
            "reply_text": reply_text,
        }
    token_budget = initial_token_budget(runtime_route)
    write_workflow_phase("realtime_reply_start", target=target.name)
    realtime_reply = maybe_build_realtime_reply(
        config=config,
        route=runtime_route,
        combined=realtime_combined,
        evidence_pack=realtime_evidence_pack,
        current_reply_text=reply_text,
        recent_reply_texts=recent_reply_texts,
    )
    write_workflow_phase("realtime_reply_done", target=target.name, applied=bool(realtime_reply.get("applied")), reason=realtime_reply.get("reason"))
    event["realtime_reply"] = realtime_reply
    if realtime_reply.get("applied"):
        decision = ReplyDecision(
            reply_text=str(realtime_reply.get("raw_reply_text") or ""),
            rule_name=str(realtime_reply.get("rule_name") or "realtime_local_reply"),
            matched=True,
            need_handoff=False,
            reason=str(realtime_reply.get("reason") or "realtime_local_reply"),
        )
        reply_text = format_reply(str(realtime_reply.get("raw_reply_text") or ""), configured_reply_prefix(config))
        event["decision"] = {
            **decision.__dict__,
            "raw_reply_text": decision.reply_text,
            "reply_text": reply_text,
        }
        clear_no_relevant_handoff_after_safe_realtime_reply(event["intent_assist"], realtime_reply)

    synthesis_enabled = bool((config.get("llm_reply_synthesis", {}) or {}).get("enabled", False))
    explicit_synthesis_override = llm_synthesis_explicit_override(config)
    deterministic_realtime_fact_reply = bool(realtime_reply.get("applied")) and str(realtime_reply.get("rule_name") or "") in {
        "realtime_catalog_price_fact",
    }
    if (
        runtime_route.get("enabled") is not False
        and (not runtime_route.get("foreground_llm_allowed") or deterministic_realtime_fact_reply)
        and not explicit_synthesis_override
    ):
        llm_synthesis = {
            "enabled": synthesis_enabled,
            "applied": False,
            "reason": "skipped_by_realtime_route",
            "route_level": runtime_route.get("level"),
            "route_reason": runtime_route.get("reason"),
            "realtime_fact_reply": deterministic_realtime_fact_reply,
        }
    else:
        synthesis_config = build_synthesis_config_for_route(config, runtime_route)
        write_workflow_phase("llm_synthesis_start", target=target.name, route_level=runtime_route.get("level"))
        llm_synthesis = maybe_synthesize_reply(
            config=synthesis_config,
            target_name=target.name,
            target_state=target_state,
            batch=batch,
            combined=combined,
            decision=decision,
            reply_text=reply_text,
            intent_assist=event["intent_assist"],
            rag_reply=rag_reply,
            llm_reply=llm_reply,
            product_knowledge=product_knowledge,
            data_capture=data_capture,
            raw_capture=raw_capture,
            customer_profile=profile,
        )
        write_workflow_phase("llm_synthesis_done", target=target.name, applied=bool(llm_synthesis.get("applied")), reason=llm_synthesis.get("reason"))
    token_budget = update_token_budget_from_synthesis(token_budget, llm_synthesis)
    event["token_budget"] = token_budget
    event["llm_reply_synthesis"] = llm_synthesis
    direct_price_fact_fallback = maybe_build_direct_product_price_fact_fallback(
        runtime_route=runtime_route,
        llm_synthesis=llm_synthesis,
        combined=realtime_combined,
        evidence_pack=realtime_evidence_pack,
        recent_reply_texts=recent_reply_texts,
    )
    if direct_price_fact_fallback.get("applied"):
        realtime_reply = direct_price_fact_fallback
        event["realtime_reply"] = realtime_reply
        llm_synthesis = {
            "enabled": synthesis_enabled,
            "applied": False,
            "reason": "skipped_by_realtime_route",
            "route_level": runtime_route.get("level"),
            "route_reason": runtime_route.get("reason"),
            "realtime_fact_reply": True,
            "replaced_handoff_reason": direct_price_fact_fallback.get("replaced_handoff_reason"),
        }
        event["llm_reply_synthesis"] = llm_synthesis
        decision = ReplyDecision(
            reply_text=str(realtime_reply.get("raw_reply_text") or ""),
            rule_name=str(realtime_reply.get("rule_name") or "realtime_catalog_price_fact"),
            matched=True,
            need_handoff=False,
            reason=str(realtime_reply.get("reason") or "product_master_direct_price_fact"),
        )
        reply_text = format_reply(str(realtime_reply.get("raw_reply_text") or ""), configured_reply_prefix(config))
        event["decision"] = {
            **decision.__dict__,
            "raw_reply_text": decision.reply_text,
            "reply_text": reply_text,
        }
    if llm_synthesis.get("applied"):
        decision = ReplyDecision(
            reply_text=str(llm_synthesis.get("raw_reply_text") or ""),
            rule_name=str(llm_synthesis.get("rule_name") or "llm_synthesis_reply"),
            matched=True,
            need_handoff=bool(llm_synthesis.get("needs_handoff")),
            reason=str(llm_synthesis.get("reason") or "guarded_llm_synthesis"),
        )
        if decision.need_handoff:
            handoff_raw = str(llm_synthesis.get("raw_reply_text") or "").strip()
            if handoff_raw:
                reply_text = format_reply(handoff_raw, configured_reply_prefix(config))
            else:
                reply_text = format_reply(handoff_acknowledgement_text(config), configured_reply_prefix(config))
        else:
            reply_text = format_reply(str(llm_synthesis.get("raw_reply_text") or ""), configured_reply_prefix(config))
            clear_no_relevant_handoff_after_safe_synthesis(event["intent_assist"], llm_synthesis)
        event["decision"] = {
            **decision.__dict__,
            "raw_reply_text": decision.reply_text,
            "reply_text": reply_text,
        }
    brain_result = event.get("customer_service_brain") if isinstance(event.get("customer_service_brain"), dict) else {}
    if should_adopt_customer_service_brain(brain_result):
        decision, reply_text = adopt_customer_service_brain_reply_event(
            event=event,
            brain_result=brain_result,
            config=config,
        )
        if decision.need_handoff and not decision.reply_text:
            return block_for_customer_service_brain_no_visible_reply(
                event=event,
                target=target,
                target_state=target_state,
                batch=batch,
                combined=combined,
                config=config,
                brain_result=brain_result,
                product_knowledge=product_knowledge,
            )
        clear_no_relevant_handoff_after_safe_brain_reply(event["intent_assist"], brain_result)
    visible_reply_before_context = reply_text
    if customer_visible_reply_is_brain_owned(event=event, decision=decision):
        reply_text = normalize_brain_owned_customer_visible_reply_text(reply_text, config=config)
    else:
        reply_text = sanitize_customer_visible_reply_text(
            reply_text,
            config=config,
            combined=combined,
            reason=str(event.get("reason") or (event.get("decision") or {}).get("reason") or decision.reason or ""),
            force_handoff_style=bool(decision.need_handoff),
            recent_reply_texts=recent_reply_texts,
        )
    if reply_text != visible_reply_before_context:
        decision = ReplyDecision(
            reply_text=split_reply_prefix(reply_text, config)[1],
            rule_name=decision.rule_name,
            matched=decision.matched,
            need_handoff=decision.need_handoff,
            reason=decision.reason,
        )
        event["decision"] = {
            **decision.__dict__,
            "raw_reply_text": decision.reply_text,
            "reply_text": reply_text,
        }
        event["visible_reply_surface_cleanup"] = {"applied": True}
    context_update = update_conversation_context_from_reply_event(target_state, event)
    if context_update:
        existing_context_update = event.get("conversation_context_update") if isinstance(event.get("conversation_context_update"), dict) else {}
        event["conversation_context_update"] = {**existing_context_update, **context_update}
        event["conversation_context_update_source"] = "customer_preference_capture+visible_reply_product_context" if existing_context_update else "visible_reply_product_context"

    brain_reply_adopted = bool(event.get("customer_service_brain_adopted"))
    style_channel = (
        "brain"
        if brain_reply_adopted
        else infer_source_channel(
            realtime_reply=realtime_reply,
            llm_synthesis=llm_synthesis,
            llm_reply=llm_reply,
            rag_reply=rag_reply,
        )
    )
    write_workflow_phase("reply_style_start", target=target.name, source_channel=style_channel)
    if brain_reply_adopted:
        style_adaptation = {
            "applied": False,
            "reason": "skipped_for_customer_service_brain",
            "reply_text": reply_text,
            "source_channel": style_channel,
        }
    else:
        style_adaptation = adapt_reply_style(
            config=config,
            customer_message=combined,
            reply_text=reply_text,
            source_channel=style_channel,
            evidence_pack=evidence_pack,
            recent_reply_texts=recent_reply_texts,
            needs_handoff=bool(decision.need_handoff),
        )
    write_workflow_phase("reply_style_done", target=target.name, applied=bool(style_adaptation.get("applied")), reason=style_adaptation.get("reason"))
    event["reply_style_adapter"] = style_adaptation
    if style_adaptation.get("applied"):
        decision = ReplyDecision(
            reply_text=str(style_adaptation.get("raw_reply_text") or ""),
            rule_name=decision.rule_name,
            matched=decision.matched,
            need_handoff=decision.need_handoff,
            reason=str(decision.reason or "") + "+style_adapter",
        )
        reply_text = str(style_adaptation.get("reply_text") or reply_text)
        event["decision"] = {
            **decision.__dict__,
            "raw_reply_text": decision.reply_text,
            "reply_text": reply_text,
        }

    event["brain_first_reply_audit"] = build_brain_first_reply_audit(event, reply_text=reply_text)

    if send:
        write_workflow_phase("freshness_check_start", target=target.name)
        freshness = detect_newer_messages_before_send(
            connector=connector,
            target=target,
            target_state=target_state,
            batch=batch,
            config=config,
        )
        write_workflow_phase(
            "freshness_check_done",
            target=target.name,
            has_newer_messages=bool(freshness.get("has_newer_messages")),
            gap_risk=bool(freshness.get("gap_risk")),
        )
        event["freshness_check"] = freshness
        if freshness.get("has_newer_messages"):
            event["action"] = "skipped"
            event["reason"] = "freshness_gap_risk" if freshness.get("gap_risk") else "newer_message_arrived_during_reply_build"
            write_runtime_status(
                "idle",
                "消息窗口连续性无法确认，本轮旧回复已暂停。" if freshness.get("gap_risk") else "客户刚刚又发了新消息，本轮旧回复已暂停，下一轮会合并最新上下文再答。",
                target=target.name,
                last_action="skipped",
                last_reason=event["reason"],
            )
            return event

    handoff_enabled = (config.get("handoff", {}) or {}).get("enabled", True) is not False
    operator_handoff_required = should_operator_handoff(
        decision,
        product_knowledge,
        fallback_allowed,
        intent_assist=event["intent_assist"],
        combined=combined,
    )
    operator_handoff = handoff_enabled and operator_handoff_required
    brain_owned_handoff_reply = (
        bool(event.get("customer_service_brain_adopted"))
        and str(decision.rule_name or "").startswith("customer_service_brain_")
        and bool(str(reply_text or "").strip())
    )
    prebuilt_handoff_reason = ""
    prebuilt_handoff_reply_text = ""
    if operator_handoff_required:
        social_handoff_redirect = ""
        if decision.rule_name == "llm_synthesis_handoff":
            social_handoff_redirect = soft_social_redirect_for_handoff(combined)
        prebuilt_handoff_reason = handoff_reason(decision, product_knowledge, intent_assist=event["intent_assist"])
        prebuilt_handoff_reply_text = build_operator_handoff_reply_text(
            config,
            decision,
            product_knowledge,
            reply_text,
            intent_assist=event["intent_assist"],
            combined=combined,
        )
        prebuilt_handoff_reply_text = _apply_greeting(
            prebuilt_handoff_reply_text,
            profile,
            config,
            target_state=target_state,
            combined=combined,
            recent_reply_texts=recent_reply_texts,
        )
        prebuilt_handoff_reply_text = sanitize_customer_visible_reply_text(
            prebuilt_handoff_reply_text,
            config=config,
            combined=combined,
            reason=prebuilt_handoff_reason,
            force_handoff_style=True,
            recent_reply_texts=recent_reply_texts,
        )
        preserve_social_handoff_redirect = bool(social_handoff_redirect)
        preserve_specific_boundary_refusal = bool(specific_boundary_refusal_for_request(combined)) and is_specific_boundary_refusal_reply(prebuilt_handoff_reply_text)
        if preserve_social_handoff_redirect:
            prebuilt_handoff_reply_text = format_reply(social_handoff_redirect, configured_reply_prefix(config))
            event["reply_style_adapter_handoff"] = {"applied": False, "reason": "social_offtopic_redirect_preserved"}
            event["outbound_naturalness_handoff"] = {"applied": False, "reason": "social_offtopic_redirect_preserved"}
            event["final_visible_llm_polish_handoff"] = {"applied": False, "reason": "social_offtopic_redirect_preserved"}
        elif preserve_specific_boundary_refusal:
            event["reply_style_adapter_handoff"] = {"applied": False, "reason": "specific_boundary_refusal_preserved"}
            event["outbound_naturalness_handoff"] = {"applied": False, "reason": "specific_boundary_refusal_preserved"}
            event["final_visible_llm_polish_handoff"] = {"applied": False, "reason": "specific_boundary_refusal_preserved"}
        elif brain_owned_handoff_reply:
            event["reply_style_adapter_handoff"] = {"applied": False, "reason": "skipped_for_customer_service_brain_handoff"}
            event["outbound_naturalness_handoff"] = {"applied": False, "reason": "skipped_for_customer_service_brain_handoff"}
            event["final_visible_llm_polish_handoff"] = {"applied": False, "reason": "deferred_to_brain_owned_handoff_reply"}
        else:
            handoff_style_adaptation = adapt_reply_style(
                config=config,
                customer_message=combined,
                reply_text=prebuilt_handoff_reply_text,
                source_channel="handoff",
                evidence_pack=evidence_pack,
                recent_reply_texts=recent_reply_texts,
                needs_handoff=True,
            )
            event["reply_style_adapter_handoff"] = handoff_style_adaptation
            if handoff_style_adaptation.get("applied"):
                prebuilt_handoff_reply_text = str(handoff_style_adaptation.get("reply_text") or prebuilt_handoff_reply_text)
            handoff_naturalness = polish_customer_visible_reply_text(
                prebuilt_handoff_reply_text,
                config=config,
                combined=combined,
                recent_reply_texts=recent_reply_texts,
            )
            event["outbound_naturalness_handoff"] = handoff_naturalness
            if handoff_naturalness.get("applied"):
                prebuilt_handoff_reply_text = str(handoff_naturalness.get("reply_text") or prebuilt_handoff_reply_text)
            if not (send and operator_handoff_required and not handoff_enabled):
                write_workflow_phase("final_polish_start", target=target.name, source_channel="handoff", reply_chars=len(prebuilt_handoff_reply_text))
                final_handoff_polish = finalize_customer_visible_reply_with_llm(
                    prebuilt_handoff_reply_text,
                    config=config,
                    combined=combined,
                    recent_reply_texts=recent_reply_texts,
                    source_channel="handoff",
                    needs_handoff=True,
                )
                write_workflow_phase("final_polish_done", target=target.name, passed=bool(final_handoff_polish.get("passed")), reason=final_handoff_polish.get("reason"))
                event["final_visible_llm_polish_handoff"] = final_handoff_polish
                if final_handoff_polish.get("passed"):
                    prebuilt_handoff_reply_text = str(final_handoff_polish.get("reply_text") or prebuilt_handoff_reply_text)
                elif final_visible_polish_blocks_send(final_handoff_polish, config=config):
                    return block_for_final_visible_polish_failure(event, target, final_handoff_polish)
                elif final_visible_polish_degraded(final_handoff_polish, config=config):
                    event["final_visible_llm_polish_handoff_degraded"] = True
        if is_identity_or_internal_probe_request(combined) and not is_identity_guard_denial_reply(prebuilt_handoff_reply_text):
            restored_identity_reply = identity_guard_denial_fallback_reply(combined, config)
            event["identity_probe_denial_guard_handoff"] = {
                "applied": True,
                "original_reply_text": prebuilt_handoff_reply_text,
                "reply_text": restored_identity_reply,
                "reason": "identity_denial_removed_by_handoff_postprocessing",
            }
            prebuilt_handoff_reply_text = restored_identity_reply
        if decision.rule_name == "customer_data_capture":
            guarded_handoff_reply = ensure_data_capture_success_context(prebuilt_handoff_reply_text, data_capture)
            if guarded_handoff_reply != prebuilt_handoff_reply_text:
                event["data_capture_reply_guard_handoff"] = {
                    "applied": True,
                    "original_reply_text": prebuilt_handoff_reply_text,
                    "reply_text": guarded_handoff_reply,
                }
                prebuilt_handoff_reply_text = guarded_handoff_reply
        handoff_reply_safety = enforce_rpa_reply_safety(prebuilt_handoff_reply_text, config)
        event["rpa_reply_safety_handoff"] = handoff_reply_safety
        if handoff_reply_safety.get("applied"):
            prebuilt_handoff_reply_text = str(handoff_reply_safety.get("reply_text") or prebuilt_handoff_reply_text)
        empty_reply_guard_handoff = ensure_non_empty_customer_visible_reply(
            prebuilt_handoff_reply_text,
            config,
            combined=combined,
            need_handoff=True,
        )
        event["empty_reply_guard_handoff"] = empty_reply_guard_handoff
        if empty_reply_guard_handoff.get("applied"):
            prebuilt_handoff_reply_text = str(empty_reply_guard_handoff.get("reply_text") or prebuilt_handoff_reply_text)
        if brain_owned_handoff_reply:
            prebuilt_handoff_reply_text = normalize_brain_owned_customer_visible_reply_text(reply_text, config=config)
            write_workflow_phase(
                "final_polish_start",
                target=target.name,
                source_channel="brain_handoff",
                reply_chars=len(prebuilt_handoff_reply_text),
            )
            final_handoff_polish = finalize_customer_visible_reply_with_llm(
                prebuilt_handoff_reply_text,
                config=config,
                combined=combined,
                recent_reply_texts=recent_reply_texts,
                source_channel="brain_handoff",
                needs_handoff=True,
            )
            write_workflow_phase(
                "final_polish_done",
                target=target.name,
                passed=bool(final_handoff_polish.get("passed")),
                reason=final_handoff_polish.get("reason"),
            )
            event["final_visible_llm_polish_handoff"] = final_handoff_polish
            if final_handoff_polish.get("passed"):
                prebuilt_handoff_reply_text = str(final_handoff_polish.get("reply_text") or prebuilt_handoff_reply_text)
                prebuilt_handoff_reply_text = normalize_brain_owned_customer_visible_reply_text(
                    prebuilt_handoff_reply_text,
                    config=config,
                )
            elif final_visible_polish_blocks_send(final_handoff_polish, config=config):
                return block_for_final_visible_polish_failure(event, target, final_handoff_polish)
            elif final_visible_polish_degraded(final_handoff_polish, config=config):
                event["final_visible_llm_polish_handoff_degraded"] = True
            event["operator_handoff_brain_reply_preserved"] = {
                "applied": True,
                "reason": "brain_first_visible_reply_authority",
            }
        decision = ReplyDecision(
            reply_text=split_reply_prefix(prebuilt_handoff_reply_text, config)[1],
            rule_name=decision.rule_name,
            matched=decision.matched,
            need_handoff=True,
            reason=prebuilt_handoff_reason,
        )
        reply_text = prebuilt_handoff_reply_text
        event["decision"] = {
            **decision.__dict__,
            "raw_reply_text": decision.reply_text,
            "reply_text": prebuilt_handoff_reply_text,
            "handoff_reason": prebuilt_handoff_reason,
        }
    if send and operator_handoff_required and not handoff_enabled:
        event["action"] = "skipped"
        event["reason"] = "operator_handoff_disabled"
        event["intent_assist"]["handoff_disabled"] = True
        return event
    if send and operator_handoff:
        reason = prebuilt_handoff_reason or handoff_reason(decision, product_knowledge, intent_assist=event["intent_assist"])
        if data_capture.get("enabled") and data_capture.get("is_customer_data"):
            data_capture["write_requested"] = write_data
            if data_capture.get("complete") and write_data:
                if customer_data_write_allowed_before_handoff(event["intent_assist"]):
                    write_customer_data_if_ready(config, target, data_capture)
                    if not data_capture.get("write_result", {}).get("ok"):
                        event["data_capture"] = data_capture
                        event["action"] = "blocked"
                        event["reason"] = "customer data was not written before operator handoff"
                        event["intent_assist"] = skipped_intent_assist(config, "customer_data_write_blocked_before_handoff")
                        return event
                else:
                    data_capture["write_skipped_reason"] = "operator_handoff_required"
            elif data_capture.get("complete"):
                data_capture["write_skipped_reason"] = "operator_handoff_required_write_data_false"
            else:
                data_capture["write_skipped_reason"] = "operator_handoff_required"
            event["data_capture"] = data_capture
        handoff_reply_text = prebuilt_handoff_reply_text or build_operator_handoff_reply_text(
            config,
            decision,
            product_knowledge,
            reply_text,
            intent_assist=event["intent_assist"],
            combined=combined,
        )
        event["decision"]["need_handoff"] = True
        event["decision"]["handoff_reason"] = reason
        event["decision"]["reply_text"] = handoff_reply_text
        write_runtime_status("thinking", f"正在向「{target.name}」发送回复。", target=target.name, reply_chars=len(handoff_reply_text))
        write_workflow_phase("rpa_send_start", target=target.name, reply_chars=len(handoff_reply_text), handoff=True)
        verified = send_reply_with_optional_multi_bubble(
            connector=connector,
            target=target,
            reply_text=handoff_reply_text,
            config=config,
        )
        write_workflow_phase("rpa_send_done", target=target.name, verified=bool(verified.get("verified")), adapter=verified.get("adapter"), state=verified.get("state"))
        event["send_result"] = verified
        event["verified"] = bool(verified.get("verified"))
        deferred = maybe_defer_transport_send_failure(target_state, message_ids, verified, event)
        if deferred:
            return deferred
        if not event["verified"]:
            event["action"] = "error"
            event["ok"] = False
            return event
        reply_trace_id = build_reply_trace_id(target.name, batch, handoff_reply_text)
        event["reply_trace_id"] = reply_trace_id
        alert = record_operator_alert(
            config=config,
            target_state=target_state,
            target=target,
            batch=batch,
            combined=combined,
            reason=reason,
            reply_text=handoff_reply_text,
            product_knowledge=product_knowledge,
        )
        mark_handoff(
            target_state,
            batch,
            reason=reason,
            status="open",
            operator_alert=alert,
            reply_trace_id=reply_trace_id,
            reply_text=handoff_reply_text,
        )
        mark_processed(target_state, batch, handoff_reply_text, reply_trace_id=reply_trace_id, send_result=verified)
        mark_coalesced_messages(
            target_state,
            selection.overflow_messages,
            reply_trace_id=reply_trace_id,
            reply_text=handoff_reply_text,
            reason="overflow_coalesced_after_handoff_reply",
        )
        record_reply_timestamp(target_state)
        finalize_data_capture_state(target_state, data_capture)
        _enqueue_post_reply_work(target, config, raw_capture, data_capture)
        event["operator_alert"] = alert
        event["action"] = "handoff_sent"
        return event

    if send and not decision.matched and not fallback_allowed:
        if handoff_enabled:
            mark_handoff(target_state, batch, reason="no_rule_matched", status="open")
            event["action"] = "handoff"
            event["reason"] = "fallback reply blocked"
        else:
            event["action"] = "skipped"
            event["reason"] = "fallback reply blocked and operator handoff disabled"
        event["intent_assist"] = skipped_intent_assist(config, "fallback_reply_blocked")
        return event

    if data_capture.get("is_customer_data") and data_capture.get("complete") and write_data:
        write_customer_data_if_ready(config, target, data_capture)

    if (
        send
        and data_capture.get("is_customer_data")
        and data_capture.get("complete")
        and not data_capture.get("write_result", {}).get("ok")
    ):
        event["data_capture"] = data_capture
        event["action"] = "blocked"
        event["reason"] = "customer data was not written; pass --write-data"
        event["intent_assist"] = skipped_intent_assist(config, "customer_data_write_blocked")
        return event

    event["data_capture"] = data_capture

    should_mark_after_data_write = bool(data_capture.get("write_result", {}).get("ok") and not send)

    if send:
        if brain_reply_adopted:
            reply_text = normalize_brain_owned_customer_visible_reply_text(reply_text, config=config)
        else:
            reply_text = _apply_greeting(
                reply_text,
                profile,
                config,
                target_state=target_state,
                combined=combined,
                recent_reply_texts=recent_reply_texts,
            )
            reply_text = sanitize_customer_visible_reply_text(
                reply_text,
                config=config,
                combined=combined,
                reason=str(event.get("reason") or event["decision"].get("reason") or ""),
                force_handoff_style=False,
                recent_reply_texts=recent_reply_texts,
            )
        if brain_reply_adopted:
            outbound_naturalness = {
                "applied": False,
                "reason": "skipped_for_customer_service_brain",
                "reply_text": reply_text,
            }
        else:
            outbound_naturalness = polish_customer_visible_reply_text(
                reply_text,
                config=config,
                combined=combined,
                recent_reply_texts=recent_reply_texts,
            )
        event["outbound_naturalness"] = outbound_naturalness
        if outbound_naturalness.get("applied"):
            reply_text = str(outbound_naturalness.get("reply_text") or reply_text)
        write_workflow_phase("final_polish_start", target=target.name, source_channel=str(style_channel or "normal"), reply_chars=len(reply_text))
        final_polish = finalize_customer_visible_reply_with_llm(
            reply_text,
            config=config,
            combined=combined,
            recent_reply_texts=recent_reply_texts,
            source_channel=str(style_channel or "normal"),
            needs_handoff=bool(decision.need_handoff),
        )
        write_workflow_phase("final_polish_done", target=target.name, passed=bool(final_polish.get("passed")), reason=final_polish.get("reason"))
        event["final_visible_llm_polish"] = final_polish
        if final_polish.get("passed"):
            reply_text = str(final_polish.get("reply_text") or reply_text)
        elif final_visible_polish_blocks_send(final_polish, config=config):
            return block_for_final_visible_polish_failure(event, target, final_polish)
        elif final_visible_polish_degraded(final_polish, config=config):
            event["final_visible_llm_polish_degraded"] = True
        if brain_reply_adopted:
            post_polish_reply = normalize_brain_owned_customer_visible_reply_text(reply_text, config=config)
        else:
            post_polish_reply = sanitize_customer_visible_reply_text(
                reply_text,
                config=config,
                combined=combined,
                reason=str(event.get("reason") or event["decision"].get("reason") or ""),
                force_handoff_style=False,
                recent_reply_texts=recent_reply_texts,
            )
        if post_polish_reply != reply_text:
            event["post_final_visible_surface_cleanup"] = {
                "applied": True,
                "original_reply_text": reply_text,
                "reply_text": post_polish_reply,
            }
            reply_text = post_polish_reply
        event["brain_first_reply_audit"] = build_brain_first_reply_audit(
            event,
            reply_text=reply_text,
            final_polish=final_polish,
        )
        if decision.rule_name == "customer_data_capture":
            guarded_data_reply = ensure_data_capture_success_context(reply_text, data_capture)
            if guarded_data_reply != reply_text:
                event["data_capture_reply_guard"] = {
                    "applied": True,
                    "original_reply_text": reply_text,
                    "reply_text": guarded_data_reply,
                }
                reply_text = guarded_data_reply
        reply_safety = enforce_rpa_reply_safety(reply_text, config)
        event["rpa_reply_safety"] = reply_safety
        if reply_safety.get("applied"):
            reply_text = str(reply_safety.get("reply_text") or reply_text)
        empty_reply_guard = ensure_non_empty_customer_visible_reply(
            reply_text,
            config,
            combined=combined,
            need_handoff=False,
        )
        event["empty_reply_guard"] = empty_reply_guard
        if empty_reply_guard.get("applied"):
            if brain_reply_adopted:
                event["empty_reply_guard"]["brain_first_blocked_local_fallback"] = True
                return block_for_customer_service_brain_no_visible_reply(
                    event=event,
                    target=target,
                    target_state=target_state,
                    batch=batch,
                    combined=combined,
                    config=config,
                    brain_result=event.get("customer_service_brain") if isinstance(event.get("customer_service_brain"), dict) else {},
                    product_knowledge=product_knowledge,
                )
            reply_text = str(empty_reply_guard.get("reply_text") or reply_text)
        event["decision"]["reply_text"] = reply_text
        event["reply_text"] = reply_text
        write_runtime_status("thinking", f"正在向「{target.name}」发送回复。", target=target.name, reply_chars=len(reply_text))
        write_workflow_phase("rpa_send_start", target=target.name, reply_chars=len(reply_text), handoff=False)
        verified = send_reply_with_optional_multi_bubble(
            connector=connector,
            target=target,
            reply_text=reply_text,
            config=config,
        )
        write_workflow_phase("rpa_send_done", target=target.name, verified=bool(verified.get("verified")), adapter=verified.get("adapter"), state=verified.get("state"))
        event["send_result"] = verified
        event["verified"] = bool(verified.get("verified"))
        deferred = maybe_defer_transport_send_failure(target_state, message_ids, verified, event)
        if deferred:
            return deferred
        if not event["verified"]:
            event["action"] = "error"
            event["ok"] = False
            return event
        reply_trace_id = build_reply_trace_id(target.name, batch, reply_text)
        event["reply_trace_id"] = reply_trace_id
        final_context_update = update_conversation_context_from_reply_event(target_state, event)
        if final_context_update:
            existing_context_update = event.get("conversation_context_update") if isinstance(event.get("conversation_context_update"), dict) else {}
            event["conversation_context_update"] = {**existing_context_update, **final_context_update}
            event["conversation_context_update_source"] = "customer_preference_capture+final_visible_reply" if existing_context_update else "final_visible_reply"
        finalize_data_capture_state(target_state, data_capture)
        record = maybe_record_rag_experience(
            target=target,
            message_ids=message_ids,
            combined=combined,
            reply_text=reply_text,
            event=event,
            reply_trace_id=reply_trace_id,
        )
        if record:
            event["rag_experience"] = record
        _enqueue_post_reply_work(target, config, raw_capture, data_capture)
        mark_processed(target_state, batch, reply_text, reply_trace_id=reply_trace_id, send_result=verified)
        mark_coalesced_messages(
            target_state,
            selection.overflow_messages,
            reply_trace_id=reply_trace_id,
            reply_text=reply_text,
            reason="overflow_coalesced_after_customer_reply",
        )
        record_reply_timestamp(target_state)
        profile_store = CustomerProfileStore()
        profile_store.increment_message_stats(target_name=target.name, is_reply=True)
        event["action"] = "sent"
        return event

    if should_mark_after_data_write:
        finalize_data_capture_state(target_state, data_capture)
        mark_processed(target_state, batch, reply_text)
        mark_coalesced_messages(
            target_state,
            selection.overflow_messages,
            reply_text=reply_text,
            reason="overflow_coalesced_after_data_write",
        )
        event["marked_processed"] = True
        event["action"] = "captured"
        return event

    if mark_dry_run:
        finalize_data_capture_state(target_state, data_capture)
        mark_processed(target_state, batch, reply_text)
        mark_coalesced_messages(
            target_state,
            selection.overflow_messages,
            reply_text=reply_text,
            reason="overflow_coalesced_after_dry_run",
        )
        event["marked_processed"] = True
    return event


def maybe_record_raw_messages(target: TargetConfig, config: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    settings = config.get("raw_messages", {}) or {}
    console_settings = config.get("_local_customer_service_settings", {}) or {}
    if console_settings.get("record_messages") is False:
        return {"enabled": False, "reason": "customer_service_record_messages_disabled"}
    if settings.get("enabled", True) is False:
        return {"enabled": False}
    normalized_messages = [item for item in messages if isinstance(item, dict) and str(item.get("content") or "").strip()]
    if not normalized_messages:
        return {"enabled": True, "ok": True, "inserted_count": 0, "duplicate_count": 0}
    try:
        store = RawMessageStore()
        allow_customer_service_learning = settings.get("allow_customer_service_learning") is True
        raw_learning_enabled = settings.get("learning_enabled", False) is True and allow_customer_service_learning
        result = store.upsert_messages(
            {
                "target_name": target.name,
                "display_name": target.name,
                "conversation_type": str(settings.get("conversation_type") or infer_raw_conversation_type(target.name)),
                "status": "active",
                "exact": target.exact,
                "record_self": True,
                "learning_enabled": raw_learning_enabled,
                "allow_learning_from_customer_service": allow_customer_service_learning,
                "notify_enabled": bool(settings.get("notify_enabled", False)),
                "source": {"type": "customer_service_listener"},
            },
            normalized_messages,
            source_module="customer_service",
            learning_enabled=raw_learning_enabled,
            create_batch=True,
            batch_reason="customer_service_poll",
        )
        if settings.get("auto_learn", False) and result.get("batch"):
            result["learning"] = {"status": "pending_enqueue", "batch_id": str(result["batch"].get("batch_id") or "")}
        return {"enabled": True, **result}
    except Exception as exc:
        return {"enabled": True, "ok": False, "error": repr(exc)}


def infer_raw_conversation_type(target_name: str) -> str:
    if target_name == FILE_TRANSFER_ASSISTANT:
        return "file_transfer"
    if "群" in target_name or "chatroom" in target_name.lower():
        return "group"
    return "private"


def known_speaker_names_for_target(target: TargetConfig, config: dict[str, Any] | None = None) -> list[str]:
    cfg = config if isinstance(config, dict) else {}
    names: list[str] = [str(target.name or "")]
    routing = cfg.get("_local_customer_service_session_routing") if isinstance(cfg.get("_local_customer_service_session_routing"), dict) else {}
    for key in ("enabled_names", "ignored_names"):
        for item in (routing or {}).get(key, []) or []:
            text = str(item or "").strip()
            if text:
                names.append(text)
    for item in cfg.get("targets", []) or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("name") or item.get("target_name") or "").strip()
        if text:
            names.append(text)
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = normalize_speaker_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(name)
    return deduped


def normalize_speaker_key(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "", str(value or "")).lower()


def handle_rate_limit_block(
    connector: WeChatConnector,
    target: TargetConfig,
    config: dict[str, Any],
    target_state: dict[str, Any],
    event: dict[str, Any],
    message_ids: list[str],
    rate_check: dict[str, Any],
) -> dict[str, Any]:
    record_rate_limit_backoff(target_state, message_ids, rate_check)
    event["action"] = "blocked"
    event["reason"] = rate_check["reason"]
    event["rate_limit"] = rate_check
    event["intent_assist"] = skipped_intent_assist(
        config,
        "rate_limited",
        {"rate_limit": rate_check},
    )
    if brain_first_requires_brain_owned_visible_reply(config):
        event["rate_limit_notice"] = {
            "suppressed": True,
            "reason": "brain_first_no_local_customer_visible_notice",
        }
        event["customer_visible_reply_blocked"] = True
        return event
    if suppress_rate_limit_notice_for_transport(connector, config):
        event["rate_limit_notice"] = {
            "suppressed": True,
            "reason": "fallback_transport_silent_defer",
        }
        return event
    if not should_send_rate_limit_notice(target_state, config, rate_check):
        return event

    reply_prefix = configured_reply_prefix(config)
    notice_text = format_reply(build_rate_limit_notice_text(config, rate_check), reply_prefix)
    final_notice_polish = finalize_customer_visible_reply_with_llm(
        notice_text,
        config=config,
        combined=str(event.get("combined_content") or ""),
        recent_reply_texts=recent_customer_visible_reply_texts(target_state),
        source_channel="rate_limit",
        needs_handoff=False,
    )
    event["final_visible_llm_polish_rate_limit"] = final_notice_polish
    if final_notice_polish.get("passed"):
        notice_text = str(final_notice_polish.get("reply_text") or notice_text)
    elif final_visible_polish_blocks_send(final_notice_polish, config=config):
        return block_for_final_visible_polish_failure(event, target, final_notice_polish)
    elif final_visible_polish_degraded(final_notice_polish, config=config):
        event["final_visible_llm_polish_rate_limit_degraded"] = True
    verified = connector.send_text_and_verify(
        target.name,
        notice_text,
        exact=target.exact,
        session_key=str(getattr(target, "session_key", "") or ""),
    )
    event["rate_limit_notice"] = {
        "reply_text": notice_text,
        "send_result": verified,
        "verified": bool(verified.get("verified")),
    }
    deferred = maybe_defer_transport_send_failure(target_state, message_ids, verified, event)
    if deferred:
        deferred["rate_limit_notice"] = event["rate_limit_notice"]
        return deferred
    if event["rate_limit_notice"]["verified"]:
        record_rate_limit_notice(target_state, message_ids, rate_check, notice_text)
        event["action"] = "rate_limit_notice_sent"
    return event


def suppress_rate_limit_notice_for_transport(connector: WeChatConnector, config: dict[str, Any]) -> bool:
    settings = config.get("rate_limits", {}) or {}
    if settings.get("notice_on_fallback_transport") is True:
        return False
    try:
        status = connector.status()  # type: ignore[attr-defined]
    except Exception:
        return False
    if not isinstance(status, dict):
        return False
    return str(status.get("adapter") or "") == "win32_ocr"


def maybe_defer_transport_send_failure(
    target_state: dict[str, Any],
    message_ids: list[str],
    verified: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any] | None:
    if verified.get("verified"):
        return None
    send_result = verified.get("send") if isinstance(verified.get("send"), dict) else verified
    if not isinstance(send_result, dict):
        return None
    state = str(send_result.get("state") or "")
    if state not in {
        "send_rate_limited",
        "send_guard_blocked",
        "send_geometry_blocked",
        "target_not_confirmed",
        "send_uia_unavailable",
        "send_input_not_ready",
    }:
        return None
    backoff = transport_send_backoff(send_result)
    record_rate_limit_backoff(target_state, message_ids, backoff)
    event["ok"] = True
    event["action"] = "deferred"
    event["reason"] = "transport_send_deferred"
    event["transport_send_backoff"] = backoff
    event["verified"] = False
    return event


def transport_send_backoff(send_result: dict[str, Any]) -> dict[str, Any]:
    guard = send_result.get("guard") if isinstance(send_result.get("guard"), dict) else {}
    rate = guard.get("rate") if isinstance(guard.get("rate"), dict) else {}
    wait_seconds = positive_int(rate.get("wait_seconds"), default=0)
    if wait_seconds <= 0:
        wait_seconds = {
            "send_rate_limited": 60,
            "send_guard_blocked": 180,
            "send_geometry_blocked": 180,
            "target_not_confirmed": 120,
            "send_uia_unavailable": 300,
            "send_input_not_ready": 180,
        }.get(str(send_result.get("state") or ""), 180)
    retry_after_at = datetime.now() + timedelta(seconds=wait_seconds)
    return {
        "allowed": False,
        "reason": "transport_" + str(send_result.get("state") or "send_failed"),
        "retry_after_at": retry_after_at.isoformat(timespec="seconds"),
        "retry_after_seconds": wait_seconds,
        "adapter": str(send_result.get("adapter") or ""),
        "send_state": str(send_result.get("state") or ""),
        "transport_error": str(send_result.get("error") or ""),
    }


def maybe_record_rag_experience(
    *,
    target: TargetConfig,
    message_ids: list[str],
    combined: str,
    reply_text: str,
    event: dict[str, Any],
    reply_trace_id: str = "",
) -> dict[str, Any] | None:
    rag_reply = event.get("rag_reply", {}) or {}
    if not rag_reply.get("applied"):
        return None
    try:
        return record_rag_reply_experience(
            target=target.name,
            message_ids=message_ids,
            question=combined,
            reply_text=reply_text,
            raw_reply_text=str(rag_reply.get("raw_reply_text") or reply_text),
            intent_assist=event.get("intent_assist", {}) or {},
            rag_reply=rag_reply,
            reply_trace_id=reply_trace_id,
        )
    except Exception as exc:
        event["rag_experience_error"] = repr(exc)
        return None


def identity_guard_enabled_for_customer_reply(config: dict[str, Any]) -> bool:
    llm_synthesis = config.get("llm_reply_synthesis", {}) or {}
    return llm_synthesis.get("identity_guard_enabled", True) is not False


def split_reply_prefix(reply_text: str, config: dict[str, Any]) -> tuple[str, str]:
    clean = str(reply_text or "").strip()
    if not clean:
        return "", ""
    prefix = configured_reply_prefix(config)
    if prefix and clean.startswith(prefix):
        return prefix, clean[len(prefix) :].strip()
    return "", clean


def strip_bot_reply_prefix_layers(reply_text: str, config: dict[str, Any], *, max_rounds: int = 4) -> str:
    text = str(reply_text or "").strip()
    if not text:
        return ""
    prefixes = [str(item).strip() for item in bot_reply_prefixes(config) if str(item).strip()]
    if not prefixes:
        return text
    rounds = max(1, int(max_rounds or 1))
    for _ in range(rounds):
        changed = False
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                changed = True
        if not changed:
            break
    return text


def has_meaningful_reply_body(text: str) -> bool:
    compact = re.sub(r"[\W_]+", "", str(text or ""), flags=re.UNICODE)
    return bool(compact)


def is_low_information_handoff_reply(reply_text: str, config: dict[str, Any]) -> bool:
    body = strip_bot_reply_prefix_layers(reply_text, config)
    return handoff_acknowledgement_is_low_information(body)


def empty_customer_visible_reply_fallback(config: dict[str, Any], *, combined: str, need_handoff: bool) -> str:
    if need_handoff:
        return handoff_acknowledgement_text(config, combined=combined)
    return "可以，我把您的问题记下，核实清楚再回复您，这样对您也更稳一点。"


def ensure_non_empty_customer_visible_reply(
    reply_text: str,
    config: dict[str, Any],
    *,
    combined: str = "",
    need_handoff: bool = False,
) -> dict[str, Any]:
    original = str(reply_text or "").strip()
    body = strip_bot_reply_prefix_layers(original, config)
    if has_meaningful_reply_body(body):
        return {"applied": False, "reason": "reply_body_nonempty", "reply_text": original}
    fallback_body = strip_bot_reply_prefix_layers(
        empty_customer_visible_reply_fallback(config, combined=combined, need_handoff=need_handoff),
        config,
    )
    if not has_meaningful_reply_body(fallback_body):
        fallback_body = "我先核实一下，稍后给您回复。"
    final = format_reply(fallback_body, configured_reply_prefix(config))
    return {
        "applied": True,
        "reason": "reply_body_empty_or_prefix_only",
        "original_reply_text": original,
        "reply_text": final,
        "need_handoff": bool(need_handoff),
    }


def rpa_reply_safety_settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("rpa_reply_safety") if isinstance(config.get("rpa_reply_safety"), dict) else {}
    guard = config.get("live_safety_guard") if isinstance(config.get("live_safety_guard"), dict) else {}
    enabled = settings.get("enabled", guard.get("enabled", False))
    return {
        "enabled": bool(enabled),
        "defer_standalone_greeting": settings.get(
            "defer_standalone_greeting",
            guard.get("defer_standalone_greeting", False),
        )
        is not False,
        "max_auto_reply_chars": int(settings.get("max_auto_reply_chars") or guard.get("max_auto_reply_chars") or 0),
    }


def normalize_greeting_probe_text(text: str) -> str:
    compact = re.sub(r"[\s，。,.！？!、~～：:；;“”\"'（）()]+", "", str(text or "")).lower()
    return compact.strip()


def is_standalone_greeting_text(text: str) -> bool:
    compact = normalize_greeting_probe_text(text)
    if not compact or len(compact) > 12:
        return False
    if any(term.lower() in compact for term in GREETING_BUSINESS_HINT_TERMS):
        return False
    return compact in STANDALONE_GREETING_TERMS


def is_repeatable_customer_probe_text(text: str) -> bool:
    compact = normalize_greeting_probe_text(text)
    if not compact:
        return False
    if compact in REPEATABLE_GREETING_PROBE_TERMS:
        return True
    return compact.startswith("在") and len(compact) <= 3


def message_has_repeatable_probe_content(message: dict[str, Any]) -> bool:
    return is_repeatable_customer_probe_text(str(message.get("content") or ""))


def processed_content_key_is_repeatable_probe(key: str) -> bool:
    parts = str(key or "").split("\x1f")
    if len(parts) != 3:
        return False
    return is_repeatable_customer_probe_text(parts[2])


def should_defer_standalone_greeting(config: dict[str, Any], batch: list[dict[str, Any]], combined: str) -> bool:
    settings = rpa_reply_safety_settings(config)
    if not settings.get("enabled") or not settings.get("defer_standalone_greeting"):
        return False
    messages = [item for item in batch if str(item.get("content") or "").strip()]
    if not messages:
        return False
    if any(not is_standalone_greeting_text(str(item.get("content") or "")) for item in messages):
        return False
    return is_standalone_greeting_text(combined)


def truncate_reply_body_for_rpa_safety(body: str, max_chars: int) -> str:
    clean = " ".join(str(body or "").split())
    if max_chars <= 0 or rpa_reply_content_char_count(clean) <= max_chars:
        return clean
    cutoff = rpa_reply_content_cutoff_index(clean, max_chars)
    preferred = -1
    for marker in ("。", "！", "？", "!", "?", "；", ";", "，", ","):
        index = clean.rfind(marker, 0, cutoff)
        if index > preferred:
            preferred = index
    if preferred >= 0 and rpa_reply_content_char_count(clean[: preferred + 1]) >= max(18, int(max_chars * 0.55)):
        trimmed = clean[: preferred + 1].rstrip()
    else:
        trimmed = clean[:cutoff].rstrip()
    return finish_truncated_reply_body(trimmed)


def finish_truncated_reply_body(text: str) -> str:
    clean = str(text or "").strip().rstrip("，,；;、:：")
    if not clean:
        return clean
    if clean.endswith(("。", "！", "？", ".", "!", "?")):
        return clean
    return clean + "。"


def rpa_reply_content_char_count(text: str) -> int:
    """Count visible content characters, ignoring punctuation and whitespace."""
    count = 0
    for char in str(text or ""):
        if not char.strip():
            continue
        category = unicodedata.category(char)
        if category.startswith("P") or category.startswith("Z"):
            continue
        count += 1
    return count


def rpa_reply_content_cutoff_index(text: str, max_chars: int) -> int:
    if max_chars <= 0:
        return 0
    count = 0
    for index, char in enumerate(str(text or "")):
        if char.strip():
            category = unicodedata.category(char)
            if not category.startswith("P") and not category.startswith("Z"):
                count += 1
        if count >= max_chars:
            return index + 1
    return len(str(text or ""))


def enforce_rpa_reply_safety(reply_text: str, config: dict[str, Any]) -> dict[str, Any]:
    settings = rpa_reply_safety_settings(config)
    if not settings.get("enabled"):
        return {"applied": False, "reason": "rpa_reply_safety_disabled", "reply_text": str(reply_text or "").strip()}
    limit = int(settings.get("max_auto_reply_chars") or 0)
    if limit <= 0:
        return {"applied": False, "reason": "max_auto_reply_chars_unset", "reply_text": str(reply_text or "").strip()}
    prefix, body = split_reply_prefix(reply_text, config)
    truncated = truncate_reply_body_for_rpa_safety(body or reply_text, limit)
    final = format_reply(truncated, prefix or configured_reply_prefix(config))
    if final.strip() == str(reply_text or "").strip():
        return {"applied": False, "reason": "within_rpa_reply_limit", "reply_text": final}
    return {
        "applied": True,
        "reason": "rpa_reply_length_capped",
        "reply_text": final,
        "max_auto_reply_chars": limit,
        "original_chars": rpa_reply_content_char_count(str(reply_text or "").strip()),
    }


def reply_multi_bubble_settings(config: dict[str, Any]) -> dict[str, Any]:
    source = config.get("reply_multi_bubble") if isinstance(config.get("reply_multi_bubble"), dict) else {}
    enabled = source.get("enabled", True) is not False
    min_split_chars = positive_int(source.get("min_split_chars"), 82)
    max_segments = max(2, min(3, positive_int(source.get("max_segments"), 3)))
    preferred_segment_chars = max(20, positive_int(source.get("preferred_segment_chars"), 54))
    max_segment_chars = max(preferred_segment_chars, positive_int(source.get("max_segment_chars"), 84))
    min_segment_chars = max(14, min(preferred_segment_chars, positive_int(source.get("min_segment_chars"), 22)))
    three_segment_threshold_chars = positive_int(
        source.get("three_segment_threshold_chars"),
        166,
    )
    inter_delay_min_ms = max(0, int(source.get("inter_segment_delay_min_ms") or 240))
    inter_delay_max_ms = max(inter_delay_min_ms, int(source.get("inter_segment_delay_max_ms") or 560))
    retry_enabled = source.get("retry_on_transient_send_failures", True) is not False
    verify_each_segment = source.get("verify_each_segment", False) is True
    raw_retry_max = source.get("max_transient_retry_per_segment")
    try:
        retry_max = int(raw_retry_max) if raw_retry_max not in (None, "") else 1
    except (TypeError, ValueError):
        retry_max = 1
    retry_max = max(0, min(2, retry_max))
    raw_retry_min = source.get("transient_retry_delay_min_ms")
    raw_retry_max_delay = source.get("transient_retry_delay_max_ms")
    try:
        retry_delay_min_ms = int(raw_retry_min) if raw_retry_min not in (None, "") else 850
    except (TypeError, ValueError):
        retry_delay_min_ms = 850
    retry_delay_min_ms = max(0, retry_delay_min_ms)
    try:
        retry_delay_max_ms = int(raw_retry_max_delay) if raw_retry_max_delay not in (None, "") else 1650
    except (TypeError, ValueError):
        retry_delay_max_ms = 1650
    retry_delay_max_ms = max(retry_delay_min_ms, retry_delay_max_ms)
    return {
        "enabled": bool(enabled),
        "min_split_chars": min_split_chars,
        "max_segments": max_segments,
        "preferred_segment_chars": preferred_segment_chars,
        "max_segment_chars": max_segment_chars,
        "min_segment_chars": min_segment_chars,
        "three_segment_threshold_chars": three_segment_threshold_chars,
        "inter_segment_delay_min_ms": inter_delay_min_ms,
        "inter_segment_delay_max_ms": inter_delay_max_ms,
        "verify_each_segment": bool(verify_each_segment),
        "retry_on_transient_send_failures": bool(retry_enabled),
        "max_transient_retry_per_segment": retry_max,
        "transient_retry_delay_min_ms": retry_delay_min_ms,
        "transient_retry_delay_max_ms": retry_delay_max_ms,
    }


def split_customer_visible_reply_for_multi_bubble(reply_text: str, config: dict[str, Any]) -> list[str]:
    raw = str(reply_text or "").strip()
    if not raw:
        return []
    settings = reply_multi_bubble_settings(config)
    if not settings.get("enabled"):
        return [raw]
    prefix, body = split_reply_prefix(raw, config)
    clean_body = " ".join(str(body or raw).split())
    content_chars = rpa_reply_content_char_count(clean_body)
    if content_chars < int(settings.get("min_split_chars") or 0):
        return [raw]

    max_segments = int(settings.get("max_segments") or 3)
    preferred_segment_chars = int(settings.get("preferred_segment_chars") or 34)
    desired_segments = 2
    if max_segments >= 3:
        three_segment_threshold = max(
            int(settings.get("three_segment_threshold_chars") or 166),
            preferred_segment_chars * 2 + int(settings.get("min_segment_chars") or 18),
        )
        if content_chars >= three_segment_threshold:
            desired_segments = 3
    desired_segments = max(2, min(max_segments, desired_segments))
    target_chars = max(
        int(settings.get("min_segment_chars") or 18),
        min(
            preferred_segment_chars,
            (content_chars + desired_segments - 1) // desired_segments,
        ),
    )
    min_segment_chars = int(settings.get("min_segment_chars") or 18)
    max_segment_chars = int(settings.get("max_segment_chars") or 52)

    units = split_reply_body_into_sendable_units(clean_body)
    if not units:
        units = [clean_body]

    packed: list[str] = []
    current = ""
    for unit in units:
        candidate = (current + unit).strip()
        current_chars = rpa_reply_content_char_count(current)
        candidate_chars = rpa_reply_content_char_count(candidate)
        should_break = (
            bool(current)
            and (
                candidate_chars > max_segment_chars
                or (
                    current_chars >= min_segment_chars
                    and current_chars >= target_chars
                    and len(packed) < desired_segments - 1
                )
            )
        )
        if should_break:
            packed.append(current.strip())
            current = unit
        else:
            current = candidate
    if current.strip():
        packed.append(current.strip())

    # For punctuation-scarce long replies, force a natural mid split so the
    # customer sees short conversational bubbles instead of one dense block.
    while len(packed) < 2 and len(packed) < max_segments and rpa_reply_content_char_count(packed[0]) > max_segment_chars:
        whole = packed.pop(0).strip()
        cutoff = rpa_reply_content_cutoff_index(whole, max(min_segment_chars, rpa_reply_content_char_count(whole) // 2))
        left = whole[:cutoff].rstrip("，,；;、")
        right = whole[cutoff:].lstrip("，,；;、")
        if not left or not right:
            packed = [whole]
            break
        packed.extend([left, right])

    if len(packed) > max_segments:
        head = packed[: max_segments - 1]
        tail = "".join(packed[max_segments - 1 :]).strip()
        packed = head + ([tail] if tail else [])

    normalized_segments: list[str] = []
    for segment in packed:
        clean = str(segment or "").strip().rstrip("，,；;、")
        if not clean:
            continue
        if not clean.endswith(("。", "！", "？", "!", "?")):
            clean = clean + "。"
        normalized_segments.append(clean)

    if len(normalized_segments) < 2:
        return [raw]

    if len(normalized_segments) > max_segments:
        normalized_segments = normalized_segments[: max_segments - 1] + ["".join(normalized_segments[max_segments - 1 :]).strip()]

    if prefix:
        first = format_reply(normalized_segments[0], prefix)
    else:
        first = normalized_segments[0]
    return [first] + normalized_segments[1:]


def split_reply_body_into_sendable_units(text: str) -> list[str]:
    """Split reply body without creating comma-led dangling bubbles.

    Multi-bubble sending should feel like a person sending several complete
    messages.  A comma split can turn "秦PLUS可以纯电跑。凯美瑞是纯油，
    不能纯电行驶。" into a standalone "不能纯电行驶。", which reads like
    a contextless fragment. Prefer sentence-level punctuation; only fall back
    to comma-like punctuation when there are no sentence boundaries at all.
    """

    clean = " ".join(str(text or "").split())
    if not clean:
        return []
    strong_units = [str(match.group(0) or "").strip() for match in re.finditer(r"[^。！？!?；;\n]+[。！？!?；;]?", clean)]
    strong_units = [item for item in strong_units if item]
    complete_units = [item for item in strong_units if rpa_reply_content_char_count(item) > 0]
    if len(complete_units) >= 2 or any(item.endswith(("。", "！", "？", "!", "?", "；", ";")) for item in complete_units):
        return complete_units
    comma_units = [str(match.group(0) or "").strip() for match in re.finditer(r"[^，,、\n]+[，,、]?", clean)]
    return [item for item in comma_units if item]


def send_reply_with_optional_multi_bubble(
    *,
    connector: WeChatConnector,
    target: TargetConfig,
    reply_text: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    segments = split_customer_visible_reply_for_multi_bubble(reply_text, config)
    settings = reply_multi_bubble_settings(config)
    if not segments:
        return {"ok": False, "verified": False, "state": "empty_reply_segment"}
    if len(segments) == 1:
        attempts = 0
        retry_attempts = 0
        single: dict[str, Any] = {}
        while True:
            attempts += 1
            write_workflow_phase(
                "rpa_send_segment_start",
                target=target.name,
                segment_index=1,
                segment_count=1,
                segment_attempt=attempts,
                reply_chars=len(segments[0]),
            )
            attempt_result = connector.send_text_and_verify(
                target.name,
                segments[0],
                exact=target.exact,
                session_key=str(getattr(target, "session_key", "") or ""),
            )
            single = dict(attempt_result) if isinstance(attempt_result, dict) else {"ok": False, "verified": False}
            verified = bool(single.get("verified"))
            state = _send_result_state(single)
            write_workflow_phase(
                "rpa_send_segment_done",
                target=target.name,
                segment_index=1,
                segment_count=1,
                segment_attempt=attempts,
                verified=verified,
                state=state,
            )
            if verified:
                break
            delay = transient_send_retry_delay_seconds(single, settings, retry_index=attempts - 1)
            if delay <= 0:
                break
            retry_attempts += 1
            write_workflow_phase(
                "rpa_send_segment_retry_wait",
                target=target.name,
                segment_index=1,
                segment_count=1,
                segment_attempt=attempts,
                retry_after_seconds=round(delay, 3),
                state=state,
            )
            time.sleep(delay)
        payload = dict(single) if isinstance(single, dict) else {"ok": False, "verified": False}
        payload.setdefault("multi_bubble", False)
        payload.setdefault("segment_count", 1)
        payload.setdefault("sent_segments", 1 if payload.get("verified") else 0)
        payload.setdefault("segments", [segments[0]])
        payload.setdefault("segment_attempt_counts", [attempts])
        payload.setdefault("retry_attempts", retry_attempts)
        return payload

    delay_min = float(int(settings.get("inter_segment_delay_min_ms") or 240)) / 1000.0
    delay_max = float(int(settings.get("inter_segment_delay_max_ms") or 560)) / 1000.0
    verify_each_segment = bool(settings.get("verify_each_segment"))
    segment_results: list[dict[str, Any]] = []
    segment_attempt_counts: list[int] = []
    sent_segments = 0
    retry_attempts = 0
    for index, segment in enumerate(segments, start=1):
        if index > 1:
            pause = random.uniform(delay_min, delay_max)
            time.sleep(max(0.0, pause))
        attempts = 0
        result: dict[str, Any] = {}
        while True:
            attempts += 1
            write_workflow_phase(
                "rpa_send_segment_start",
                target=target.name,
                segment_index=index,
                segment_count=len(segments),
                segment_attempt=attempts,
                reply_chars=len(segment),
            )
            should_verify_segment = verify_each_segment or index == len(segments)
            skip_segment_rate_guard = index > 1
            if should_verify_segment or not callable(getattr(connector, "send_text", None)):
                result = connector.send_text_and_verify(
                    target.name,
                    segment,
                    exact=target.exact,
                    skip_send_rate_guard=skip_segment_rate_guard,
                    session_key=str(getattr(target, "session_key", "") or ""),
                )
            else:
                send_only = connector.send_text(
                    target.name,
                    segment,
                    exact=target.exact,
                    skip_send_rate_guard=skip_segment_rate_guard,
                    session_key=str(getattr(target, "session_key", "") or ""),
                )  # type: ignore[attr-defined]
                send_only_meta = send_only if isinstance(send_only, dict) else {}
                result = {
                    "ok": bool(send_only_meta.get("ok")),
                    "verified": bool(send_only_meta.get("ok")),
                    "send": send_only_meta,
                    "verification_mode": "send_only_intermediate",
                    "adapter": send_only_meta.get("adapter"),
                    "state": send_only_meta.get("state"),
                    "skip_send_rate_guard": skip_segment_rate_guard,
                }
            verified = bool(result.get("verified"))
            state = _send_result_state(result)
            write_workflow_phase(
                "rpa_send_segment_done",
                target=target.name,
                segment_index=index,
                segment_count=len(segments),
                segment_attempt=attempts,
                verified=verified,
                state=state,
            )
            if verified:
                break
            delay = transient_send_retry_delay_seconds(result, settings, retry_index=attempts - 1)
            if delay <= 0:
                break
            retry_attempts += 1
            write_workflow_phase(
                "rpa_send_segment_retry_wait",
                target=target.name,
                segment_index=index,
                segment_count=len(segments),
                segment_attempt=attempts,
                retry_after_seconds=round(delay, 3),
                state=state,
            )
            time.sleep(delay)
        segment_results.append(result)
        segment_attempt_counts.append(attempts)
        if not bool(result.get("verified")):
            break
        sent_segments += 1

    last = segment_results[-1] if segment_results else {}
    send_meta = last.get("send") if isinstance(last.get("send"), dict) else last
    send_meta = send_meta if isinstance(send_meta, dict) else {}
    all_verified = sent_segments == len(segments)
    payload: dict[str, Any] = {
        "ok": bool(all_verified),
        "verified": bool(all_verified),
        "multi_bubble": True,
        "verification_strategy": "verify_each_segment" if verify_each_segment else "verify_final_segment_only",
        "segments": segments,
        "segment_count": len(segments),
        "sent_segments": sent_segments,
        "segment_results": segment_results,
        "segment_attempt_counts": segment_attempt_counts,
        "retry_attempts": retry_attempts,
        "send": send_meta,
        "messages": last.get("messages"),
    }
    if not all_verified:
        failed_index = min(len(segments) - 1, sent_segments)
        payload["failed_segment_index"] = failed_index + 1
        payload["failed_segment_text"] = segments[failed_index]
    for key in ("adapter", "state", "guard", "error"):
        if key in send_meta:
            payload[key] = send_meta.get(key)
    return payload


TRANSIENT_SEND_RETRYABLE_STATES = {
    "send_rate_limited",
    "send_guard_blocked",
    "send_geometry_blocked",
    "target_not_confirmed",
    "send_uia_unavailable",
    "send_lock_timeout",
}


def _send_result_meta(result: dict[str, Any]) -> dict[str, Any]:
    send_meta = result.get("send") if isinstance(result.get("send"), dict) else result
    return send_meta if isinstance(send_meta, dict) else {}


def _send_result_state(result: dict[str, Any]) -> str:
    send_meta = _send_result_meta(result)
    state = str(send_meta.get("state") or "").strip()
    if state:
        return state
    nested = send_meta.get("send_result") if isinstance(send_meta.get("send_result"), dict) else {}
    nested_state = str(nested.get("state") or "").strip()
    if nested_state:
        return nested_state
    return str(result.get("state") or "").strip()


def transient_send_retry_delay_seconds(
    result: dict[str, Any],
    settings: dict[str, Any],
    *,
    retry_index: int,
) -> float:
    if settings.get("retry_on_transient_send_failures") is not True:
        return 0.0
    max_retry = int(settings.get("max_transient_retry_per_segment") or 0)
    if retry_index >= max_retry:
        return 0.0
    state = _send_result_state(result)
    if state not in TRANSIENT_SEND_RETRYABLE_STATES:
        return 0.0
    delay_min = float(int(settings.get("transient_retry_delay_min_ms") or 850)) / 1000.0
    delay_max = float(int(settings.get("transient_retry_delay_max_ms") or 1650)) / 1000.0
    if state == "send_lock_timeout":
        # Foreground/lock contention usually recovers quickly; use shorter
        # retry jitter to reduce customer-visible tail latency.
        delay_min = min(delay_min, 0.22)
        delay_max = min(delay_max, 0.68)
        if delay_max < delay_min:
            delay_max = delay_min
    delay = random.uniform(max(0.0, delay_min), max(max(0.0, delay_min), delay_max))
    send_meta = _send_result_meta(result)
    guard = send_meta.get("guard") if isinstance(send_meta.get("guard"), dict) else {}
    rate = guard.get("rate") if isinstance(guard.get("rate"), dict) else {}
    try:
        wait_seconds = float(rate.get("wait_seconds") or 0.0)
    except (TypeError, ValueError):
        wait_seconds = 0.0
    if wait_seconds > 0:
        delay = max(delay, min(wait_seconds + 0.2, 6.0))
    return delay


def recent_customer_visible_reply_texts(target_state: dict[str, Any], *, limit: int = 5) -> list[str]:
    replies: list[tuple[str, str]] = []
    for item in target_state.get("sent_replies", []) or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("reply_text") or "").strip()
        if text and state_context_item_is_usable(item, text=text, allow_polluted_message_contents=True):
            replies.append((str(item.get("processed_at") or ""), text))
    for item in target_state.get("handoff_events", []) or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("reply_text") or "").strip()
        if text and state_context_item_is_usable(item, text=text, timestamp_key="created_at"):
            replies.append((str(item.get("created_at") or ""), text))
    replies.sort(key=lambda item: item[0])
    return [text for _, text in replies[-max(1, limit) :]]


def recent_customer_message_texts(target_state: dict[str, Any], *, limit: int = 5) -> list[str]:
    messages: list[str] = []
    for item in target_state.get("sent_replies", []) or []:
        if not isinstance(item, dict):
            continue
        if not state_context_item_is_usable(item):
            continue
        for content in item.get("message_contents", []) or []:
            text = str(content or "").strip()
            if text and not state_context_text_is_polluted(text):
                messages.append(text)
    return messages[-max(1, limit) :]


def state_context_item_is_usable(
    item: dict[str, Any],
    *,
    text: str = "",
    timestamp_key: str = "processed_at",
    allow_polluted_message_contents: bool = False,
) -> bool:
    timestamp = parse_datetime(str(item.get(timestamp_key) or item.get("processed_at") or item.get("created_at") or ""))
    if timestamp is not None and datetime.now() - timestamp > timedelta(hours=6):
        return False
    if text and state_context_text_is_polluted(text):
        return False
    if allow_polluted_message_contents:
        return True
    for content in item.get("message_contents", []) or []:
        if state_context_text_is_polluted(str(content or "")):
            return False
    return True


def state_context_text_is_polluted(text: str) -> bool:
    if text_has_test_marker(text) or text_has_model_reply_marker(text):
        return True
    return bool(message_learning_exclusion_reason({"content": text, "type": "text"}, source_module="state_context_probe"))


def build_realtime_context_combined(combined: str, target_state: dict[str, Any]) -> str:
    current = str(combined or "").strip()
    fresh_need = extract_latest_fresh_need_reset_message(current)
    if fresh_need:
        return fresh_need
    recent = recent_customer_message_texts(target_state, limit=5)
    if not current or not recent:
        return current
    if state_context_text_is_polluted(current):
        return current
    clean = re.sub(r"\s+", "", current)
    context_markers = (
        "这个预算",
        "刚才",
        "你刚才",
        "这两台",
        "这几台",
        "这个方向",
        "按我的情况",
        "重新收一下",
        "哪两台",
        "哪台",
        "具体车源",
        "直接挑",
        "挑两台",
        "挑几台",
        "给我两台",
        "给我几台",
        "别再问预算",
        "不用再问预算",
        "按这个",
        "就按这个",
        "按前面",
        "前面说的",
        "这俩",
        "它俩",
        "哪个更适合",
        "哪台更适合",
        "新手停车",
        "停车不熟",
        "父母",
        "老人",
        "异地",
        "置换",
    )
    business_context_markers = context_markers + (
        "车源",
        "推荐",
        "挑",
        "筛",
        "看看",
        "预算",
        "自动挡",
        "倒车影像",
        "影像",
        "雷达",
        "省油",
        "家用",
        "通勤",
        "接娃",
        "老婆",
        "女士",
        "好停",
        "好开",
        "试驾",
        "到店",
        "周末",
    )
    lacks_budget = not re.search(r"\d+(?:\.\d+)?\s*(?:到|-|~|至)?\s*\d*(?:\.\d+)?\s*万", clean)
    needs_context = any(marker in clean for marker in context_markers) or (
        lacks_budget and any(marker in clean for marker in business_context_markers)
    )
    if not needs_context:
        return current
    recent_text = "\n".join(f"- {text}" for text in recent[-4:])
    return f"近期客户需求：\n{recent_text}\n当前客户问题：{current}"


def extract_latest_fresh_need_reset_message(text: str) -> str:
    """Return a latest self-contained new-customer need from a mixed visible batch."""
    raw = str(text or "").strip()
    if "\n" not in raw:
        return ""
    lines = [line.strip(" \t-•·、") for line in raw.splitlines() if line.strip()]
    if len(lines) <= 1:
        return ""
    for line in reversed(lines):
        clean = re.sub(r"\s+", "", line).lower()
        if not any(marker in clean for marker in ("刚加上", "刚加好友", "刚加微信", "刚通过", "朋友介绍", "第一次咨询")):
            continue
        has_budget = bool(
            re.search(
                r"(\d+(?:\.\d+)?\s*(?:到|至|~|～|-|—|－)?\s*\d*(?:\.\d+)?\s*万|[一二三四五六七八九十两]{1,3}万)",
                line,
            )
        )
        has_vehicle_need = any(
            marker in clean
            for marker in (
                "预算",
                "买",
                "换",
                "看车",
                "车",
                "家用",
                "通勤",
                "接待",
                "客户",
                "代步",
                "省油",
                "体面",
            )
        )
        if has_budget and has_vehicle_need:
            return line
    return ""


def recent_sent_reply_content_keys(target_state: dict[str, Any], *, limit: int = 30) -> set[str]:
    """Track exact customer-visible replies so File Transfer self-tests do not re-read them as customer input."""
    keys: list[str] = []
    # This is a duplicate-suppression guard, not a context provider. Do not apply
    # the six-hour context freshness filter here, otherwise old visible self
    # replies in File Transfer Assistant can be re-read as new customer input.
    for collection, text_keys in (
        (target_state.get("sent_replies", []) or [], ("reply_text", "raw_reply_text")),
        (target_state.get("handoff_events", []) or [], ("reply_text",)),
        (target_state.get("operator_alerts", []) or [], ("reply_text",)),
    ):
        for item in collection[-max(1, limit) :]:
            if not isinstance(item, dict):
                continue
            for text_key in text_keys:
                key = normalize_reply_content_key(str(item.get(text_key) or ""))
                if key:
                    keys.append(key)
    return set(keys)


def normalize_reply_content_key(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or "").strip()).lower()


def has_explicit_handoff_phrase(text: str) -> bool:
    clean = re.sub(r"\s+", "", str(text or ""))
    if not clean:
        return False
    return any(re.search(pattern, clean, re.I) for pattern in EXPLICIT_HANDOFF_PATTERNS)


def choose_customer_visible_variant(
    variants: list[str],
    *,
    context: str,
    recent_reply_texts: list[str] | None = None,
) -> str:
    reply, _ = choose_reply_variant(variants, key_text=context, recent_reply_texts=recent_reply_texts)
    return de_template_reply_text(reply, key_text=context, recent_reply_texts=recent_reply_texts)


def new_energy_usage_detail(context: str) -> str:
    clean = re.sub(r"\s+", "", str(context or ""))
    if not clean:
        return "您这个顾虑我先记下"
    patterns = (
        r"(?:一天|每天|平时|通勤|上下班|来回|单程).{0,12}?(\d{1,3})\s*(?:公里|km|KM)",
        r"(\d{1,3})\s*(?:公里|km|KM).{0,12}?(?:一天|每天|平时|通勤|上下班|来回|单程)",
    )
    for pattern in patterns:
        match = re.search(pattern, clean, re.I)
        if match:
            return f"您说的{match.group(1)}公里这个用车强度我记下了"
    if any(term in clean for term in ("家用", "通勤", "上下班", "代步", "跑高速", "老婆", "孩子")):
        return "您的用车场景我先记下"
    return "您这个顾虑我先记下"


def is_location_contact_context(context: str) -> bool:
    text = str(context or "")
    return any(term in text for term in LOCATION_CONTACT_STRONG_TERMS) and any(
        term in text for term in LOCATION_VISIT_CONTEXT_TERMS
    )


def is_same_day_delivery_context(context: str) -> bool:
    clean = re.sub(r"\s+", "", str(context or ""))
    if not clean:
        return False
    return any(term in clean for term in SAME_DAY_DELIVERY_STRONG_TERMS)


def current_handoff_decision_context(*, combined: str = "", reason: str = "") -> str:
    """Use only the latest customer turn when choosing a concealed handoff note.

    The full combined transcript can contain prior self replies with prices,
    product names, or procedural wording. Letting those old self fragments drive
    handoff wording turns the guard into a second answer engine and can override
    Brain's current-turn understanding.
    """

    current = current_customer_text(combined).strip()
    lines = [strip_leading_group_speaker_line(line.strip()) for line in current.splitlines() if line.strip()]
    if len(lines) >= 2:
        current = lines[-1]
    context = f"{current} {reason}".strip()
    return context or str(reason or combined or "").strip()


def concealed_handoff_reply(*, combined: str = "", reason: str = "", recent_reply_texts: list[str] | None = None) -> str:
    context = current_handoff_decision_context(combined=combined, reason=reason)
    if is_location_contact_context(context):
        return choose_customer_visible_variant(
            [
                "门店地址和到了找谁我先确认一下，确认好后发您，避免您导航错或到店没人对接。",
                "这个我给您核清楚再发，地址、导航和到店联系人都一起确认好，省得您白跑。",
                "可以，我先确认门店地址和到店对接人，核好后回您，您过去会更稳一点。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in context for term in CONTACT_DATA_TERMS) and any(term in context for term in APPOINTMENT_TERMS):
        if any(term in context for term in ("旧车", "置换", "开过去", "抵车款")):
            return choose_customer_visible_variant(
                [
                    "可以，姓名、电话和到店时间我记下了，旧车置换也一起备注。我这边确认车源和门店排期，核好后回您。",
                    "联系方式、到店时间和旧车置换我都记下了。接下来确认车源状态、门店排期和看车安排，核好后回您。",
                    "信息我记下了，旧车也按置换一起备注。这边确认车源还在不在、到店时间能不能排上，弄清楚后回您。",
                ],
                context=context,
                recent_reply_texts=recent_reply_texts,
            )
        return choose_customer_visible_variant(
            [
                "可以，姓名、电话和到店时间我记下了。我这边确认一下车源和门店排期，弄清楚后回您，尽量别让您白跑。",
                "可以，联系方式和到店时间我记下，接下来确认车源状态、门店排期和看车安排，核好后回您。",
                "信息我记下了，这边确认车源还在不在、到店时间能不能排上，弄清楚后回您。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in context for term in NEW_ENERGY_TERMS):
        usage_detail = new_energy_usage_detail(context)
        return choose_customer_visible_variant(
            [
                f"您担心电池和三电很正常，{usage_detail}。这块不能只听一句口头保证，我先核实检测记录、电池状态和车况，请稍等，确认后再跟您说这台适不适合。",
                f"新能源最该看的就是电池、三电和检测记录。{usage_detail}，我先核清楚实际续航、检测报告和车况，请稍等，确认好再给您更稳的判断。",
                f"这个问题问得很关键。{usage_detail}，混动车要看电池状态、三电检测和实际用车强度，我先把这些核实清楚，请稍等，确认后再跟您说适不适合入手。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    if is_same_day_delivery_context(context):
        return choose_customer_visible_variant(
            [
                "这个要看车源状态、手续资料、付款方式、过户和临牌安排，不能只凭一句话保证当天提。我先把这些环节核清楚，确认能不能当天办完再回复您。",
                "当天提车不是不能安排，但要先确认车况报告、合同资料、付款到账、过户和临牌这些节点。我先核实门店流程，确认稳了再跟您说。",
                "您想当天办完我理解，这个我先确认车源、手续资料和过户排期，能不能当天交车要核准后再给您准话，避免您白跑或等太久。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    has_finance_boundary = any(term in context for term in FINANCE_HARD_BOUNDARY_TERMS)
    has_condition_boundary = any(term in context for term in CONDITION_BOUNDARY_TERMS)
    if has_finance_boundary and has_condition_boundary:
        return choose_customer_visible_variant(
            [
                "贷款要看资方审核，检测报告和车况记录我可以帮您核；事故、水泡、火烧这类不能先口头定死，最终以报告和门店确认为准。",
                "金融方案要按资方审核走，车况这块我会重点核检测报告、出险和维保记录；涉及事故水泡火烧，最终以报告为准。",
                "这两块要分开看：贷款看资方审核，车况看检测报告和实车记录。我先把报告边界和金融方案核清楚，再给您准话。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in context for term in DOCUMENT_TERMS):
        if any(term in context for term in ("少开", "低开", "金额")):
            return choose_customer_visible_variant(
                [
                    "我理解您是想把流程提前问清楚，合同和发票金额这块必须按实际交易和门店流程走，不能随口答应调整。我先确认合同流程和开票要求，核清楚再回复您。",
                    "这个我先帮您问清楚，发票金额和合同信息要按实际交易来确认，不能直接口头定。您稍等，我问下领导后再给您明确说法。",
                    "这块确实要提前确认好，免得后面来回改。合同签署和开票金额都要按流程核实，您稍等一下，我确认清楚后回您。",
                ],
                context=context,
                recent_reply_texts=recent_reply_texts,
            )
        return choose_customer_visible_variant(
            [
                "可以，这块我帮您问清楚。合同和发票需要按门店流程来，我把开票抬头、税号和合同资料要求核清楚后回您，避免后面填错。",
                "这个提前问是对的，我帮您确认合同流程和开票资料要求后再回复您，省得后面资料来回补。",
                "合同和发票这块我先确认一下门店流程。您稍等，我核实好抬头、税号和需要准备的资料后回复您。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    has_price_boundary = any(term in context for term in PRICE_ONLY_HARD_BOUNDARY_TERMS)
    if has_finance_boundary and has_price_boundary:
        return choose_customer_visible_variant(
            [
                "贷款要看资方审核，成交价也要按门店流程核准；我先把付款方案和可确认的边界核清楚，再给您稳妥答复。",
                "金融审批和最终成交条件不能先口头定死。我先按车源、付款方式和门店流程确认，核准后再回复您。",
                "这类问题我会谨慎点：贷款看资方，价格看正式核准。我先确认付款方案和可谈范围，再跟您说准。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    if has_finance_boundary:
        return choose_customer_visible_variant(
            [
                "贷款要看资方审核，首付、月供和利率都得按具体车源和资料核算；我先确认金融方案后给您准话。",
                "金融这块不能先口头定死，主要看车型、首付比例、期数和资方审核。我先把方案核清楚再回复您。",
                "分期可以先算方向，但审批结果、利率和月供要看资方。我先确认付款方案，核好后跟您说准。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    if has_price_boundary:
        return choose_customer_visible_variant(
            [
                "价格和优惠我会帮您核，但超出公开规则的部分不能直接口头答应。我先按门店流程确认，再给您明确答复。",
                "价格我可以帮您确认和争取，但最低价或破例优惠不能直接口头保证。我核准后再回复您。",
                "这个我先按正式流程核一下，可谈范围和最终成交条件确认后，再给您准话。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    if is_same_day_delivery_context(context):
        return choose_customer_visible_variant(
            [
                "这个要看车源状态、手续资料、付款方式、过户和临牌安排，不能只凭一句话保证当天提。我先把这些环节核清楚，确认能不能当天办完再回复您。",
                "当天提车不是不能安排，但要先确认车况报告、合同资料、付款到账、过户和临牌这些节点。我先核实门店流程，确认稳了再跟您说。",
                "您想当天办完我理解，这个我先确认车源、手续资料和过户排期，能不能当天交车要核准后再给您准话，避免您白跑或等太久。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in context for term in APPOINTMENT_TERMS):
        return choose_customer_visible_variant(
            [
                "您这个安排我记下了，我这边确认排期，核实后回您。",
                "可以，我把您想看的时间和车型记下，确认一下车源和门店排期再回复您。",
                "到店这块我记一下，再确认排期和车源状态，避免您白跑，确认好就回您。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in context for term in AFTER_SALES_TERMS):
        return choose_customer_visible_variant(
            [
                "这类问题我需要先核实关键细节，再给您准确处理意见，请稍等我回复您。",
                "这个不能只凭一句话下结论，我核实车况和相关记录后，再给您准确说法。",
                "涉及车况承诺我会谨慎一点，先把检测和记录核实清楚，再给您明确回复。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in context for term in TRADE_IN_TERMS):
        return choose_customer_visible_variant(
            [
                "可以先做个大概区间。旧车这边我先按车型、年份、公里数、上牌地和车况信息记录，您再补下配置、过户次数、保养和事故水泡火烧情况，估价会更准。",
                "置换流程没问题，我先按年份、公里数、上牌地和车况给您看大概区间。您再发下配置、过户次数、保养和事故水泡火烧情况，我核实行情后判断会更准。",
                "这台车信息已经有基础了，可以先估一版。您把配置版本、车况瑕疵、有没有出险水泡火烧，再加外观内饰照片发我，我按检测和行情给您看区间。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in context for term in INTERNAL_PROBE_TERMS):
        return choose_customer_visible_variant(
            [
                "内部规则这些肯定不能外发，您别介意；您关心车源、车况或置换哪块，我继续帮您核实。",
                "这类内部信息不能外发。您要是担心回复不准，可以直接问具体车况、价格或手续，我按实际情况给您确认。",
                "内部提示词这些不方便发，您别介意；您要看车或置换，我继续帮您把关键点核清楚。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    if any(term in context for term in OFF_TOPIC_TERMS):
        return choose_customer_visible_variant(
            [
                "这个不属于看车业务，我这边不能处理。您如果是看车、车况或置换问题，我再按实际情况帮您核实。",
                "这类事情我这边不接，咱们还是回到车源、车况或置换上，我按实际需求帮您确认。",
                "这个方向不合适聊。您把买车或卖车需求说一下，我继续给您筛车或核实车况。",
            ],
            context=context,
            recent_reply_texts=recent_reply_texts,
        )
    return choose_customer_visible_variant(
            [
                "这个问题我需要先核实一下细节。您的需求我记下了，确认好就回您。",
                "这点不能直接乱说，得把情况核实清楚再回复您。我记下，确认后回您。",
                "这个我需要再确认一下细节，确认好了再回复您，避免给您说错。",
        ],
        context=context,
        recent_reply_texts=recent_reply_texts,
    )


def sanitize_customer_visible_reply_text(
    reply_text: str,
    *,
    config: dict[str, Any],
    combined: str = "",
    reason: str = "",
    force_handoff_style: bool = False,
    recent_reply_texts: list[str] | None = None,
) -> str:
    prefix, body = split_reply_prefix(reply_text, config)
    if not body:
        return str(reply_text or "").strip()
    cleaned_body = normalize_visible_reply_surface_noise(body, evidence_pack={})
    if cleaned_body != body:
        reply_text = format_reply(cleaned_body, prefix)
        body = cleaned_body
    if not identity_guard_enabled_for_customer_reply(config):
        return str(reply_text or "").strip()
    # Preserve social off-topic soft redirect text even when the caller requests
    # handoff-style concealment; otherwise it gets overwritten into a generic
    # "核实负责人" template and loses conversational quality.
    if force_handoff_style:
        social_redirect = soft_social_redirect_for_handoff(combined)
        if social_redirect:
            return format_reply(social_redirect, prefix or configured_reply_prefix(config))
        specific_refusal = specific_boundary_refusal_for_request(combined)
        if specific_refusal:
            return format_reply(specific_refusal, prefix or configured_reply_prefix(config))
        if is_specific_boundary_refusal_reply(body):
            return format_reply(body, prefix or configured_reply_prefix(config))
        if is_identity_or_internal_probe_request(combined) and is_identity_guard_denial_reply(body):
            return format_reply(body, prefix or configured_reply_prefix(config))
    if not force_handoff_style:
        if has_explicit_handoff_phrase(body):
            softened_body = soften_customer_visible_handoff_terms(body)
            softened_body = normalize_customer_visible_mechanics(
                softened_body,
                combined=combined,
                recent_reply_texts=recent_reply_texts,
            )
            return format_reply(softened_body, prefix or configured_reply_prefix(config))
        return str(reply_text or "").strip()
    safe_body = concealed_handoff_reply(combined=combined, reason=reason, recent_reply_texts=recent_reply_texts)
    effective_prefix = prefix or configured_reply_prefix(config)
    return format_reply(safe_body, effective_prefix)


def is_identity_or_internal_probe_request(text: str) -> bool:
    current = re.sub(r"\s+", "", current_customer_text(text))
    if not current:
        return False
    if any(term in current for term in INTERNAL_PROBE_TERMS):
        return True
    explicit_identity_terms = (
        "是不是ai",
        "是不是AI",
        "是ai吗",
        "是AI吗",
        "ai自动",
        "AI自动",
        "ai回",
        "AI回",
        "自动回复",
        "机器人",
        "机器客服",
        "智能客服",
        "人工智能",
    )
    return any(term in current for term in explicit_identity_terms)


def is_identity_guard_denial_reply(text: str) -> bool:
    clean = re.sub(r"\s+", "", str(text or ""))
    if not clean:
        return False
    denial_markers = ("不是AI", "不是ai", "不是机器人", "不是自动回复", "不是机器乱回")
    boundary_markers = ("内部规则", "系统提示词", "提示词", "内部信息", "不能外发", "不方便发", "不方便提供")
    if any(marker in clean for marker in denial_markers):
        return False
    return any(marker in clean for marker in boundary_markers) and not exposes_ai_identity_in_customer_reply(clean)


def identity_guard_denial_fallback_reply(text: str, config: dict[str, Any]) -> str:
    current = re.sub(r"\s+", "", current_customer_text(text))
    prefix = configured_reply_prefix(config)
    if any(term in current for term in ("系统提示词", "内部规则", "api密钥", "API密钥", "api key", "密钥", "prompt")):
        return format_reply(
            "内部规则这些不能外发，您别介意；您有具体需求的话，我按实际情况帮您核实。",
            prefix,
        )
    return format_reply(
        "我这边在看消息，涉及具体承诺不会随口定；您关心哪块，我按实际情况帮您确认。",
        prefix,
    )


def is_specific_boundary_refusal_reply(text: str) -> bool:
    clean = re.sub(r"\s+", "", str(text or ""))
    if not clean:
        return False
    markers = (
        "不能帮",
        "不能处理",
        "不建议",
        "不合规",
        "违法",
        "违规",
        "交易真实性",
        "真实车况",
        "正常流程",
        "如实",
        "公里数",
        "里程",
        "内部信息",
        "不能提供",
    )
    return any(marker in clean for marker in markers)


def specific_boundary_refusal_for_request(text: str) -> str:
    current = re.sub(r"\s+", "", current_customer_text(text))
    if not current:
        return ""
    odometer_terms = ("调低公里", "改公里", "改里程", "调里程", "里程表", "公里数")
    tamper_terms = ("调低", "改低", "改少", "做低", "处理一下", "帮我调", "帮我改")
    if any(term in current for term in odometer_terms) and any(term in current for term in tamper_terms):
        return "这个不能帮您做，也不建议这么处理，容易影响交易真实性和后续责任。咱们还是按真实车况、正常流程来；需要的话我可以帮您核实车况，再看怎么合规评估或置换。"
    if "公里数" in current and ("高" in current or "多" in current) and ("再卖" in current or "卖车" in current):
        return "公里数这块要如实披露，不能帮您做不真实处理。可以走正常检测和评估，我帮您核实车况后，再看怎么合规置换或出售。"
    return ""


def normalize_customer_visible_surface_only(reply_text: str, *, config: dict[str, Any]) -> str:
    prefix, body = split_reply_prefix(reply_text, config)
    if not body:
        return str(reply_text or "").strip()
    cleaned_body = normalize_visible_reply_surface_noise(body, evidence_pack={})
    return format_reply(cleaned_body, prefix or configured_reply_prefix(config))


def soften_customer_visible_handoff_terms(text: str) -> str:
    clean = str(text or "")
    replacements = (
        ("转人工确认", "帮您确认"),
        ("转人工处理", "帮您继续处理"),
        ("转人工", "请负责同事接着确认"),
        ("人工客服", "负责同事"),
        ("真人客服", "负责同事"),
        ("销售人工确认", "门店确认"),
        ("人工确认", "核实确认"),
        ("人工核实", "核实确认"),
        ("请联系顾问", "我帮您确认"),
    )
    for src, dst in replacements:
        clean = clean.replace(src, dst)
    return clean


def polish_customer_visible_reply_text(
    reply_text: str,
    *,
    config: dict[str, Any],
    combined: str = "",
    recent_reply_texts: list[str] | None = None,
) -> dict[str, Any]:
    settings = config.get("reply_naturalness", {}) if isinstance(config.get("reply_naturalness"), dict) else {}
    if settings.get("enabled", True) is False:
        return {"applied": False, "reason": "reply_naturalness_disabled", "reply_text": str(reply_text or "").strip()}
    prefix, body = split_reply_prefix(reply_text, config)
    if not body:
        return {"applied": False, "reason": "empty_reply", "reply_text": str(reply_text or "").strip()}
    polished = de_template_reply_text(body, key_text=combined, recent_reply_texts=recent_reply_texts)
    polished = normalize_customer_visible_mechanics(polished, combined=combined, recent_reply_texts=recent_reply_texts)
    polished = diversify_similar_customer_visible_reply(polished, combined=combined, recent_reply_texts=recent_reply_texts)
    if not polished or polished.strip() == body.strip():
        return {"applied": False, "reason": "no_naturalness_delta", "reply_text": str(reply_text or "").strip()}
    guard = guard_customer_visible_polish(base_reply=body, polished_reply=polished)
    final_reply = format_reply(polished, prefix or configured_reply_prefix(config)) if guard.get("allowed") else str(reply_text or "").strip()
    return {
        "applied": bool(guard.get("allowed")),
        "reason": "customer_visible_polished" if guard.get("allowed") else str(guard.get("reason") or "polish_guard_rejected"),
        "reply_text": final_reply,
        "raw_reply_text": polished if guard.get("allowed") else body,
        "guard": guard,
    }


def final_visible_polish_speed_settings(config: dict[str, Any]) -> dict[str, Any]:
    polish = config.get("final_visible_llm_polish") if isinstance(config.get("final_visible_llm_polish"), dict) else {}
    short_skip_enabled = polish.get("skip_short_reply_fast_path_enabled", False) is True
    short_skip_max_chars = positive_int(polish.get("skip_short_reply_max_chars"), 46)
    short_skip_max_sentences = positive_int(polish.get("skip_short_reply_max_sentences"), 2)
    final_cap = positive_int(polish.get("max_reply_chars"), 620)
    rpa_cap = int(rpa_reply_safety_settings(config).get("max_auto_reply_chars") or 0)
    brain = config.get("customer_service_brain") if isinstance(config.get("customer_service_brain"), dict) else {}
    brain_cap = positive_int(brain.get("quality_reply_max_chars"), 0) if brain else 0
    cap_candidates = [final_cap]
    if rpa_cap > 0:
        cap_candidates.append(rpa_cap)
    if brain_cap > 0:
        cap_candidates.append(brain_cap)
    effective_cap = min(cap_candidates)
    return {
        "short_skip_enabled": bool(short_skip_enabled),
        "short_skip_max_chars": short_skip_max_chars,
        "short_skip_max_sentences": short_skip_max_sentences,
        "effective_polish_chars_cap": max(1, effective_cap),
    }


def customer_visible_sentence_count(text: str) -> int:
    fragments = [item for item in re.split(r"[。！？!?；;\n]+", str(text or "")) if re.sub(r"[\s，,、]+", "", item)]
    return len(fragments)


def pre_cap_reply_body_for_final_polish(body: str, config: dict[str, Any]) -> str:
    clean = str(body or "").strip()
    if not clean:
        return clean
    cap = int(final_visible_polish_speed_settings(config).get("effective_polish_chars_cap") or 0)
    if cap <= 0:
        return clean
    return truncate_reply_body_for_rpa_safety(clean, cap)


def should_fast_skip_final_visible_polish(
    *,
    reply_body: str,
    config: dict[str, Any],
    source_channel: str,
    needs_handoff: bool,
) -> bool:
    # Brain First baseline requires every customer-visible reply to pass
    # through final polish. Optimize latency around the LLM path, not by
    # bypassing polish for short greetings or simple answers.
    return False


def finalize_customer_visible_reply_with_llm(
    reply_text: str,
    *,
    config: dict[str, Any],
    combined: str = "",
    recent_reply_texts: list[str] | None = None,
    source_channel: str = "normal",
    needs_handoff: bool = False,
) -> dict[str, Any]:
    prefix, body = split_reply_prefix(reply_text, config)
    draft_body = pre_cap_reply_body_for_final_polish(body or str(reply_text or "").strip(), config)
    if should_fast_skip_final_visible_polish(
        reply_body=draft_body,
        config=config,
        source_channel=source_channel,
        needs_handoff=needs_handoff,
    ):
        return {
            "enabled": bool((config.get("final_visible_llm_polish", {}) or {}).get("enabled", False)),
            "required": bool((config.get("final_visible_llm_polish", {}) or {}).get("required_for_send", False)),
            "applied": False,
            "passed": True,
            "source_channel": source_channel,
            "reason": "final_visible_llm_polish_fast_local_skip",
            "raw_reply_text": draft_body,
            "reply_text": format_reply(draft_body, prefix or configured_reply_prefix(config)),
            "duration_seconds": 0.0,
        }
    result = maybe_polish_customer_visible_reply(
        config=config,
        customer_message=combined,
        reply_text=draft_body,
        recent_reply_texts=recent_reply_texts or [],
        source_channel=source_channel,
        needs_handoff=needs_handoff,
    )
    if result.get("passed"):
        polished_body = str(result.get("reply_text") or draft_body or reply_text).strip()
        polished_body = pre_cap_reply_body_for_final_polish(polished_body, config)
        result["reply_text"] = format_reply(polished_body, prefix or configured_reply_prefix(config))
    return result


def final_visible_polish_blocks_send(result: dict[str, Any], *, config: dict[str, Any] | None = None) -> bool:
    if not bool(result.get("enabled") and result.get("required") and not result.get("passed")):
        return False
    if final_visible_polish_degraded(result, config=config):
        return False
    return True


def final_visible_polish_degraded(result: dict[str, Any], *, config: dict[str, Any] | None = None) -> bool:
    if not isinstance(config, dict):
        return False
    settings = config.get("final_visible_llm_polish")
    if not isinstance(settings, dict):
        return False
    if settings.get("allow_send_when_unavailable", False) is not True:
        return False
    return final_visible_polish_is_degradable_failure(result)


def final_visible_polish_is_degradable_failure(result: dict[str, Any]) -> bool:
    reason = str(result.get("reason") or "").lower()
    llm_status = result.get("llm_status") if isinstance(result.get("llm_status"), dict) else {}
    fallback_reply = str(result.get("reply_text") or "").strip()
    if "polish_exposed_handoff_marker" in reason:
        return False
    if fallback_reply and llm_status.get("ok") is False:
        # Final visible polish is a last-mile wording pass on top of an already
        # safe draft. If the polish provider/model/auth/config fails, we should
        # fall back to the draft instead of failing the whole customer reply.
        return True
    if (reason.startswith("polish_") or reason.startswith("polished_")) and fallback_reply:
        # Guard rejected only the polished variant; fallback draft reply stays
        # unchanged and is safer than blocking runtime delivery.
        return True
    if fallback_reply and (
        "final_polish_guard_rejected" in reason
        or "invalid_request_error" in reason
        or "unsupported" in reason
        or "api key is not set" in reason
        or "unauthorized" in reason
        or "forbidden" in reason
    ):
        return True
    status_value = llm_status.get("status")
    status_code = 0
    try:
        status_code = int(status_value)
    except (TypeError, ValueError):
        status_code = 0
    if status_code in {408, 409, 429, 500, 502, 503, 504}:
        return True

    transient_tokens = (
        "timeout",
        "timed out",
        "connection aborted",
        "connection reset",
        "connection closed",
        "connection error",
        "remote disconnected",
        "remote end closed",
        "server disconnected",
        "temporarily unavailable",
        "service unavailable",
        "too many requests",
        "rate limit",
        "gateway timeout",
        "bad gateway",
        "llm_polish_unavailable",
    )
    if any(token in reason for token in transient_tokens):
        return True
    llm_error = str(llm_status.get("error") or "").lower()
    if llm_error and any(token in llm_error for token in transient_tokens):
        return True
    return False


def block_for_final_visible_polish_failure(event: dict[str, Any], target: TargetConfig, result: dict[str, Any]) -> dict[str, Any]:
    event["action"] = "blocked"
    event["ok"] = False
    event["reason"] = "final_visible_llm_polish_failed"
    write_runtime_status(
        "blocked",
        "发送前AI轻润色未通过，已暂停自动回复，避免把未润色模板直接发给客户。",
        target=target.name,
        last_action="blocked",
        last_reason=str(result.get("reason") or "final_visible_llm_polish_failed"),
    )
    return event


def normalize_customer_visible_mechanics(
    reply_text: str,
    *,
    combined: str = "",
    recent_reply_texts: list[str] | None = None,
) -> str:
    text = str(reply_text or "").strip()
    if not text:
        return text
    replacements: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("我会转给销售人工确认", ("我再核一下排期和安排", "这边确认好排期再回您", "我问清楚安排再回您")),
        ("我会转给人工继续处理", ("我需要内部确认后再继续处理", "这块我确认好再往下处理", "我问清楚后再接着处理")),
        ("转接人工客服", ("帮您转给负责的人", "这边安排负责的人跟进", "我帮您把问题递过去")),
        ("销售人工确认", ("负责人确认", "门店确认", "负责的人确认")),
        ("人工确认", ("负责人确认", "内部确认", "核实确认")),
        ("人工核实", ("负责人核实", "内部核实", "核实确认")),
        ("稍后继续处理", ("后面继续跟进", "接下来继续处理", "我这边继续往下跟")),
        ("稍后给您准确回复", ("确认后再回复您", "问清楚后回您", "核完再回您")),
        ("当前无法直接确认", ("现在还不能直接确认", "这点不能直接定", "这个得先核一下")),
        ("我当前无法直接确认", ("我现在还不能直接确认", "这点我不能直接定", "这个我得先核一下")),
        ("这个问题需要", ("这块需要", "这个得", "这点要")),
        ("我会重点看", ("重点看", "我主要看", "这边先核")),
        ("我会优先看", ("优先看", "我主要看", "这边先看")),
        ("我再帮您", ("我再", "这边再", "后面再")),
        ("我这边会", ("这边会", "我会", "先")),
        ("这个需求已经比较明确了", ("条件已经比较清楚了", "这个方向挺明确了", "需求这块已经不算泛了")),
        ("好的，请发我", ("可以，您发我", "行，您把", "您直接发我")),
        ("没问题，您先慢慢看", ("可以，您先慢慢看", "行，您先慢慢看，不急", "没事，您先慢慢看")),
        ("我帮您确认报价", ("我帮您核报价", "我给您核一下报价", "我这边看下报价")),
        ("我帮您核对", ("我帮您看下", "我这边核一下", "我来对一下")),
        ("您这个需求我建议先按", ("您这个需求可以先按", "这类需求重点按", "这种情况先按")),
        ("您把预算和主要用途发我", ("预算和主要用途您发我一下", "您说下预算和主要用途", "预算、主要用途这两点您补一下")),
    )
    for old, variants in replacements:
        if old not in text:
            continue
        replacement, _ = choose_reply_variant(
            list(variants),
            key_text=f"{combined}|naturalness|{old}",
            recent_reply_texts=recent_reply_texts,
        )
        text = text.replace(old, replacement, 1)
    text = re.sub(r"^(收到|好的|可以|没问题)[，,。]\s*(收到|好的|可以|没问题)[，,。]\s*", r"\1，", text)
    return text


def diversify_similar_customer_visible_reply(
    reply_text: str,
    *,
    combined: str = "",
    recent_reply_texts: list[str] | None = None,
) -> str:
    text = str(reply_text or "").strip()
    recent_reply_texts = recent_reply_texts or []
    if not text or not recent_reply_texts:
        return text
    max_similarity = max((reply_similarity(text, recent) for recent in recent_reply_texts[-4:]), default=0.0)
    if max_similarity < 0.72:
        return text
    variants = [
        re.sub(r"^可以[，,]", "行，", text),
        re.sub(r"^收到[，,]", "好，", text),
        text.replace("这个需求已经比较明确了", "条件已经比较清楚了", 1),
        text.replace("按您说的", "照您刚才说的", 1),
        text.replace("您这个需求", "这类需求", 1),
        text.replace("我这边", "这边", 1),
        text.replace("我会", "先", 1),
        text.replace("我帮您", "我来", 1),
    ]
    candidates = [item.strip() for item in variants if item.strip() and item.strip() != text]
    if not candidates:
        return text
    selected, _ = choose_reply_variant(candidates, key_text=f"{combined}|similarity-polish", recent_reply_texts=recent_reply_texts)
    selected_similarity = max((reply_similarity(selected, recent) for recent in recent_reply_texts[-4:]), default=0.0)
    return selected if selected_similarity <= max_similarity else text


def guard_customer_visible_polish(*, base_reply: str, polished_reply: str) -> dict[str, Any]:
    base_numbers = protected_customer_visible_tokens(base_reply)
    polished_numbers = protected_customer_visible_tokens(polished_reply)
    new_numbers = sorted(polished_numbers - base_numbers)
    if new_numbers:
        return {"allowed": False, "reason": "polish_introduced_number", "new_numbers": new_numbers}
    if exposes_ai_identity_in_customer_reply(polished_reply):
        return {"allowed": False, "reason": "polish_exposed_ai_identity"}
    return {"allowed": True}


def protected_customer_visible_tokens(text: str) -> set[str]:
    value = str(text or "")
    tokens = set(re.findall(r"\d+(?:\.\d+)?\s*(?:万|元|块|公里|km|KM|年|天|%|折)", value))
    tokens.update(re.findall(r"1[3-9]\d{9}", value))
    tokens.update(re.findall(r"\b\d{4,}\b", value))
    return {re.sub(r"\s+", "", item) for item in tokens if item}


def exposes_ai_identity_in_customer_reply(text: str) -> bool:
    clean = re.sub(r"\s+", "", str(text or ""))
    if not clean:
        return False
    markers = ("我是AI", "我是ai", "我是机器人", "AI助手", "智能客服", "自动回复系统")
    for marker in markers:
        marker_clean = re.sub(r"\s+", "", marker)
        if marker_clean not in clean:
            continue
        if any(prefix + marker_clean in clean for prefix in ("不是", "并不是", "不算")):
            continue
        return True
    return False


def maybe_build_direct_product_price_fact_fallback(
    *,
    runtime_route: dict[str, Any],
    llm_synthesis: dict[str, Any],
    combined: str,
    evidence_pack: dict[str, Any],
    recent_reply_texts: list[str] | None,
) -> dict[str, Any]:
    """Recover public product-master price answers when legacy synthesis over-handoffs."""
    if str(runtime_route.get("reason") or "") != "direct_product_price_requires_synthesis":
        return {"applied": False, "reason": "not_direct_product_price_route"}
    if not bool(llm_synthesis.get("applied")) or not bool(llm_synthesis.get("needs_handoff")):
        return {"applied": False, "reason": "synthesis_not_handoff"}
    replaced_reason = str(llm_synthesis.get("reason") or "")
    guard_reason = str(((llm_synthesis.get("guard") or {}) or {}).get("reason") or "")
    if replaced_reason not in {"sales_followup_requires_handoff", "llm_guard_blocked"} and guard_reason != "sales_followup_requires_handoff":
        return {"applied": False, "reason": "handoff_reason_not_price_fallback_safe"}
    reply, variant_index, used_product_ids = build_direct_catalog_price_reply(
        combined,
        evidence_pack,
        recent_reply_texts=recent_reply_texts,
    )
    if not reply or not used_product_ids:
        return {"applied": False, "reason": "no_product_master_direct_price_fact"}
    return {
        "applied": True,
        "rule_name": "realtime_catalog_price_fact",
        "reason": "product_master_direct_price_fact",
        "raw_reply_text": reply,
        "reply_text": reply,
        "variant_index": variant_index,
        "used_product_ids": used_product_ids,
        "saved_reason": "product_master_price_fact_recovered_from_synthesis_handoff",
        "replaced_handoff_reason": guard_reason or replaced_reason,
    }


def should_operator_handoff(
    decision: ReplyDecision,
    product_knowledge: dict[str, Any] | None,
    fallback_allowed: bool,
    intent_assist: dict[str, Any] | None = None,
    combined: str = "",
) -> bool:
    if evidence_requires_handoff(intent_assist):
        if brain_decision_clears_soft_evidence_handoff(decision, intent_assist):
            return False
        return True
    # Intent-assist handoff only overrides when no safe rule matched or the
    # handoff reason is an explicit signal (e.g. appointment after data capture).
    if isinstance(intent_assist, dict) and intent_assist.get("needs_handoff"):
        if not decision.matched or decision.need_handoff:
            return True
        reason = str(intent_assist.get("reason") or "")
        if reason == "customer_data_complete_with_appointment":
            if decision.rule_name == "customer_data_capture" and customer_data_complete_can_auto_ack(combined):
                return False
            return True
    if product_knowledge and product_knowledge.get("auto_reply_allowed") is False:
        return True
    if product_knowledge and product_knowledge.get("needs_handoff"):
        return True
    if decision.need_handoff:
        return True
    if not decision.matched and not fallback_allowed:
        return True
    if decision.reason in {"approval_required", "no_rule_matched"}:
        return True
    return False


def brain_decision_clears_soft_evidence_handoff(
    decision: ReplyDecision,
    intent_assist: dict[str, Any] | None,
) -> bool:
    """Keep old evidence-safety hints from overruling a Brain-owned reply.

    `no_relevant_business_evidence` is advisory in Brain First mode: greetings,
    small talk, and common-sense answers may legitimately have no product/formal
    evidence. Hard approval/risk reasons still require handoff.
    """

    if not decision.matched or decision.need_handoff:
        return False
    if not str(decision.rule_name or "").startswith("customer_service_brain_"):
        return False
    safety = evidence_safety(intent_assist)
    reasons = {str(item) for item in safety.get("reasons", []) or [] if str(item)}
    soft_advisory_handoff_reasons = {
        "matched_faq_requires_handoff",
        "missing_authoritative_evidence",
        "no_relevant_business_evidence",
        "auto_reply_disabled",
    }
    return bool(reasons) and reasons <= soft_advisory_handoff_reasons


def llm_synthesis_explicit_override(config: dict[str, Any]) -> bool:
    settings = config.get("llm_reply_synthesis", {}) or {}
    if not settings.get("enabled", False):
        return False
    if settings.get("force_foreground") is True:
        return True
    return str(settings.get("provider") or "") == "manual_json" and isinstance(settings.get("candidate"), dict)


def build_operator_handoff_reply_text(
    config: dict[str, Any],
    decision: ReplyDecision,
    product_knowledge: dict[str, Any] | None,
    current_reply_text: str,
    intent_assist: dict[str, Any] | None = None,
    combined: str = "",
) -> str:
    if decision.rule_name == "llm_synthesis_handoff" and str(current_reply_text or "").strip():
        social_redirect = soft_social_redirect_for_handoff(combined)
        if social_redirect:
            return format_reply(social_redirect, configured_reply_prefix(config))
        return current_reply_text
    if decision.rule_name in {"customer_service_brain_handoff", "customer_service_brain_reply"} and str(current_reply_text or "").strip():
        return current_reply_text
    if decision.rule_name == "llm_synthesis_reply" and llm_reply_already_handoff_style(current_reply_text):
        return current_reply_text
    if evidence_requires_handoff(intent_assist):
        return format_reply(handoff_acknowledgement_text(config, combined=combined), configured_reply_prefix(config))
    if product_knowledge and product_knowledge.get("auto_reply_allowed") is False:
        return format_reply(handoff_acknowledgement_text(config, combined=combined), configured_reply_prefix(config))
    if product_knowledge and product_knowledge.get("reply_text"):
        return current_reply_text
    return format_reply(handoff_acknowledgement_text(config, combined=combined), configured_reply_prefix(config))


def brain_handoff_reply_should_be_preserved(reply_text: str, *, combined: str) -> bool:
    """Keep Brain's concrete cautious answer instead of flattening it to a generic handoff."""

    reply = str(reply_text or "").strip()
    if not reply or handoff_acknowledgement_is_low_information(reply):
        return False
    compact_reply = compact_handoff_match_text(reply)
    compact_question = compact_handoff_match_text(combined)
    if not compact_reply or not compact_question:
        return False
    specific_terms = (
        "第二排",
        "后排",
        "后备厢",
        "后备箱",
        "放倒",
        "装载",
        "尺寸",
        "实车",
        "现场",
        "检测报告",
        "车况",
        "贷款",
        "金融",
        "置换",
        "保险",
        "保单",
        "资方",
        "合同",
        "定金",
        "过户",
    )
    if any(compact_handoff_match_text(term) in compact_question and compact_handoff_match_text(term) in compact_reply for term in specific_terms):
        return True
    stop_terms = {
        "这个",
        "那个",
        "一下",
        "能不能",
        "可以",
        "需要",
        "怎么",
        "什么",
        "多少",
        "现在",
        "这里",
        "客户",
    }
    tokens = [
        token
        for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", str(combined or ""))
        if token not in stop_terms and len(compact_handoff_match_text(token)) >= 2
    ]
    return any(compact_handoff_match_text(token) in compact_reply for token in tokens[:12])


def compact_handoff_match_text(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()【】\[\]<>《》]+", "", str(text or "")).lower()


def soft_social_redirect_for_handoff(combined: str) -> str:
    current = re.sub(r"\s+", "", current_customer_text(combined))
    if not current:
        return ""
    social_terms = (
        "天气",
        "笑话",
        "吃饭",
        "午饭",
        "晚饭",
        "电影",
        "电视剧",
        "音乐",
        "宠物",
        "旅游",
        "游戏",
        "八卦",
    )
    if not any(term in current for term in social_terms):
        return ""
    business_terms = (
        "预算",
        "推荐",
        "车",
        "看车",
        "试驾",
        "价格",
        "优惠",
        "贷款",
        "分期",
        "置换",
        "过户",
    )
    if any(term in current for term in business_terms):
        return ""
    return "哈哈，这个话题我就不乱接梗了，天气信息以实时天气为准。咱们先把看车需求聊明白：预算、用途、是否置换这三点给我，我马上继续帮您筛车。"


def handoff_acknowledgement_text(config: dict[str, Any], *, combined: str = "") -> str:
    settings = config.get("handoff", {}) or {}
    conceal_handoff = identity_guard_enabled_for_customer_reply(config)
    text = str(
        settings.get("acknowledgement_reply")
        or "这个问题我当前无法直接确认，我把情况记下，核实清楚后再回复您。"
    )
    if handoff_acknowledgement_is_formulaic(text):
        if handoff_acknowledgement_is_low_information(text):
            return concise_handoff_confirmation_text(combined=combined, conceal_handoff=conceal_handoff)
        if str(combined or "").strip() and conceal_handoff:
            return concealed_handoff_reply(combined=combined, reason="")
        if "直接按" in str(combined or ""):
            if conceal_handoff:
                return "这类价格我不能直接确认，需要按正式流程核准后再跟您说准。"
            return "这类价格我不能直接确认，需要正式核准后才能给准话。"
        if conceal_handoff:
            return "这点我不能直接替您定，我把问题记下，核实清楚后再回您。"
        return "这点我不能直接替您定，我把问题记下，让负责的同事核实后再回您。"
    return text


def handoff_acknowledgement_is_formulaic(text: str) -> bool:
    formulaic_terms = (
        "收到，我先看一下",
        "收到我先看一下",
        "收到先看一下",
        "我先看一下",
        "我先看下",
        "收到，我先记录",
        "稍后继续处理",
        "请示上级",
        "这个问题需要销售人工确认，我先帮您记录并提醒同事跟进",
        "我先帮您记录并提醒同事跟进",
        "当前无法直接确认，我先帮您记录",
        "不能直接确认",
        "问清楚负责人",
    )
    return any(term in str(text or "") for term in formulaic_terms)


def handoff_acknowledgement_is_low_information(text: str) -> bool:
    value = re.sub(r"[，,。.!！？、\s]+", "", str(text or ""))
    if not value:
        return True
    markers = ("收到我先看一下", "收到我先看下", "收到先看一下", "我先看一下", "我先看下", "先看一下", "先看下")
    return any(marker in value for marker in markers) and len(value) <= 14


def concise_handoff_confirmation_text(*, combined: str = "", conceal_handoff: bool = True) -> str:
    context = re.sub(r"\s+", "", str(combined or ""))
    has_any = lambda terms: any(term in context for term in terms)
    if is_location_contact_context(context):
        return "收到，我先确认门店地址和到店对接，稍后回您。"
    has_specific_price_target = bool(re.search(r"\d+(?:\.\d+)?(?:万|整)", context))
    if (has_specific_price_target and has_any(("最低", "底价", "能不能给", "给到", "谈到", "便宜点", "少点"))) or has_any(PRICE_HARD_BOUNDARY_TERMS):
        return "收到，这块价格我先核一下，稍后给您准话。"
    if has_any(CONTACT_DATA_TERMS) and has_any(APPOINTMENT_TERMS):
        return "收到，您的信息我记下了，我先核排期，稍后回您。"
    if conceal_handoff:
        return "收到，我先核一下，稍后给您明确回复。"
    return "收到，这个我先确认下，稍后给您回复。"


def llm_reply_already_handoff_style(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return False
    markers = ("人工", "同事", "销售", "核实", "复核", "转接", "联系", "请示", "负责人", "确认后")
    return any(marker in clean for marker in markers)


def handoff_reason(
    decision: ReplyDecision,
    product_knowledge: dict[str, Any] | None,
    intent_assist: dict[str, Any] | None = None,
) -> str:
    evidence_reason = evidence_handoff_reason(intent_assist)
    if evidence_reason:
        return evidence_reason
    if product_knowledge and product_knowledge.get("approval_reason"):
        return str(product_knowledge.get("approval_reason"))
    if product_knowledge and product_knowledge.get("auto_reply_allowed") is False:
        return str(product_knowledge.get("reason") or "auto_reply_disabled")
    if product_knowledge and product_knowledge.get("needs_handoff"):
        return str(product_knowledge.get("reason") or "product_knowledge_requires_handoff")
    return str(decision.reason or "operator_handoff")


def record_operator_alert(
    config: dict[str, Any],
    target_state: dict[str, Any],
    target: TargetConfig,
    batch: list[dict[str, Any]],
    combined: str,
    reason: str,
    reply_text: str,
    product_knowledge: dict[str, Any],
) -> dict[str, Any]:
    settings = config.get("operator_alert", {}) or {}
    alert = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "target": target.name,
        "message_ids": [reply_input_message_identity(item) for item in batch if reply_input_message_identity(item)],
        "message_contents": [str(item.get("content") or "") for item in batch],
        "combined_content": combined,
        "reason": reason,
        "reply_text": reply_text,
        "product_knowledge": product_knowledge,
        "status": "open",
    }
    if settings.get("enabled", True):
        alert_path = resolve_path(settings.get("alert_log_path") or "runtime/logs/wechat_customer_service/operator_alerts.jsonl")
        append_jsonl(alert_path, alert)
        alert["delivery"] = {"type": "jsonl", "path": str(alert_path), "ok": True}
    else:
        alert["delivery"] = {"type": "disabled", "ok": False}
    alert["case_store"] = create_handoff_case(config, alert)
    dispatch = alert["case_store"].get("dispatch") if isinstance(alert.get("case_store"), dict) else None
    if isinstance(dispatch, dict) and isinstance(alert.get("delivery"), dict):
        alert["delivery"]["feishu"] = dispatch
    target_state.setdefault("operator_alerts", []).append(alert)
    target_state["operator_alerts"] = target_state["operator_alerts"][-MAX_STORED_IDS:]
    return alert


def brain_failure_alert_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("customer_service_brain_failure_alert")
    settings = raw if isinstance(raw, dict) else {}
    return {
        "enabled": settings.get("enabled", True) is not False,
        "window_seconds": parse_positive_int(settings.get("window_seconds"), 600, minimum=30, maximum=86400),
        "threshold": parse_positive_int(settings.get("threshold"), 3, minimum=1, maximum=20),
        "cooldown_seconds": parse_positive_int(settings.get("cooldown_seconds"), 600, minimum=30, maximum=86400),
    }


def parse_iso_timestamp_seconds(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def customer_service_brain_failure_reason(brain_result: dict[str, Any] | None) -> str:
    payload = brain_result if isinstance(brain_result, dict) else {}
    if not payload.get("enabled") or str(payload.get("mode") or "") != "brain_first":
        return ""
    reason = str(payload.get("reason") or "").strip()
    rule_name = str(payload.get("rule_name") or "").strip()
    if rule_name in {"customer_service_brain_safe_fallback", "customer_service_brain_no_visible_reply"}:
        return reason or rule_name
    if reason in BRAIN_FAILURE_ALERT_REASONS:
        return reason
    if payload.get("error"):
        return reason or "customer_service_brain_error"
    if not payload.get("applied") or not payload.get("adoptable"):
        return reason or "customer_service_brain_not_adopted"
    return ""


def block_for_customer_service_brain_no_visible_reply(
    *,
    event: dict[str, Any],
    target: TargetConfig,
    target_state: dict[str, Any],
    batch: list[dict[str, Any]],
    combined: str,
    config: dict[str, Any],
    brain_result: dict[str, Any] | None,
    product_knowledge: dict[str, Any] | None,
) -> dict[str, Any]:
    """Stop outbound sending when Brain First has no Brain-authored reply.

    Guard, quality gates, and legacy routes may provide feedback or internal
    operator context, but they must never become the customer-visible answer
    engine.  Blocking here also prevents an already computed legacy/default
    decision from leaking after Brain failure.
    """

    brain_payload = brain_result if isinstance(brain_result, dict) else {}
    reason = customer_service_brain_failure_reason(brain_payload) or "customer_service_brain_not_adopted"
    previous_decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
    event["blocked_legacy_decision_before_brain"] = previous_decision
    event["customer_service_brain_adopted"] = {
        "applied": False,
        "mode": brain_payload.get("mode"),
        "rule_name": str(brain_payload.get("rule_name") or "customer_service_brain_no_visible_reply"),
        "reason": reason,
    }
    event["decision"] = {
        "reply_text": "",
        "raw_reply_text": "",
        "rule_name": "customer_service_brain_no_visible_reply",
        "matched": False,
        "need_handoff": True,
        "reason": reason,
    }
    event["action"] = "blocked"
    event["ok"] = False
    event["reason"] = "customer_service_brain_no_visible_reply"
    event["customer_visible_reply_blocked"] = True
    event["brain_first_reply_audit"] = build_brain_first_reply_audit(event, reply_text="")
    if not bool(event.get("dry_run")):
        alert = record_operator_alert(
            config=config,
            target_state=target_state,
            target=target,
            batch=batch,
            combined=combined,
            reason=f"customer_service_brain_no_visible_reply:{reason}",
            reply_text="",
            product_knowledge=product_knowledge or {},
        )
        mark_handoff(
            target_state,
            batch,
            reason=f"customer_service_brain_no_visible_reply:{reason}",
            status="open",
            operator_alert=alert,
            reply_text="",
        )
        event["operator_alert"] = alert
    write_runtime_status(
        "idle",
        f"「{target.name}」本轮未获得 Brain 生成的可发送回复，已暂停出站并保留人工处理记录。",
        target=target.name,
        last_action="blocked",
        last_reason=reason,
    )
    return event


def adopt_customer_service_brain_reply_event(
    *,
    event: dict[str, Any],
    brain_result: dict[str, Any],
    config: dict[str, Any],
) -> tuple[ReplyDecision, str]:
    decision = ReplyDecision(
        reply_text=str(brain_result.get("raw_reply_text") or ""),
        rule_name=str(brain_result.get("rule_name") or "customer_service_brain_reply"),
        matched=True,
        need_handoff=bool(brain_result.get("needs_handoff")),
        reason=str(brain_result.get("reason") or "customer_service_brain_adopted"),
    )
    reply_text = format_reply(decision.reply_text, configured_reply_prefix(config))
    event["customer_service_brain_adopted"] = {
        "applied": True,
        "mode": brain_result.get("mode"),
        "rule_name": decision.rule_name,
        "reason": decision.reason,
    }
    event["decision"] = {
        **decision.__dict__,
        "raw_reply_text": decision.reply_text,
        "reply_text": reply_text,
    }
    return decision, reply_text


def maybe_record_customer_service_brain_failure_alert(
    *,
    config: dict[str, Any],
    target_state: dict[str, Any],
    target: TargetConfig,
    batch: list[dict[str, Any]],
    combined: str,
    brain_result: dict[str, Any] | None,
    product_knowledge: dict[str, Any],
) -> dict[str, Any]:
    settings = brain_failure_alert_settings(config)
    reason = customer_service_brain_failure_reason(brain_result)
    if not settings["enabled"]:
        return {"enabled": False, "failure": bool(reason), "reason": reason}
    now = datetime.now()
    now_text = now.isoformat(timespec="seconds")
    window_seconds = int(settings["window_seconds"])
    cutoff = now.timestamp() - window_seconds
    history_key = "customer_service_brain_failure_events"
    retained: list[dict[str, Any]] = []
    for raw in target_state.get(history_key, []) or []:
        item = raw if isinstance(raw, dict) else {}
        ts = parse_iso_timestamp_seconds(item.get("created_at"))
        if ts is not None and ts >= cutoff:
            retained.append(item)
    if reason:
        retained.append({"created_at": now_text, "reason": reason})
    target_state[history_key] = retained[-MAX_STORED_IDS:]
    if not reason:
        return {
            "enabled": True,
            "failure": False,
            "window_seconds": window_seconds,
            "count": len(retained),
            "threshold": int(settings["threshold"]),
        }

    threshold = int(settings["threshold"])
    cooldown_seconds = int(settings["cooldown_seconds"])
    last_alert_ts = parse_iso_timestamp_seconds(target_state.get("last_customer_service_brain_failure_alert_at"))
    cooldown_passed = last_alert_ts is None or (now.timestamp() - last_alert_ts) >= cooldown_seconds
    threshold_reached = len(retained) >= threshold
    summary = {
        "enabled": True,
        "failure": True,
        "reason": reason,
        "window_seconds": window_seconds,
        "count": len(retained),
        "threshold": threshold,
        "cooldown_seconds": cooldown_seconds,
        "threshold_reached": threshold_reached,
        "cooldown_passed": cooldown_passed,
        "alert_created": False,
    }
    if threshold_reached and cooldown_passed:
        alert = record_operator_alert(
            config=config,
            target_state=target_state,
            target=target,
            batch=batch,
            combined=combined,
            reason=f"customer_service_brain_unhealthy:{reason}",
            reply_text="客服大脑主链路连续失败，已转人工关注。",
            product_knowledge=product_knowledge,
        )
        target_state["last_customer_service_brain_failure_alert_at"] = now_text
        summary["alert_created"] = True
        summary["operator_alert"] = alert
    return summary


def dispatch_handoff_case_notification(case: dict[str, Any]) -> dict[str, Any]:
    if bool(case.get("deduped")):
        return {
            "enabled": False,
            "status": "deduped_skip",
            "adapter": "feishu",
            "reason": "handoff_case_already_exists",
            "case_id": case.get("case_id"),
        }
    try:
        from apps.wechat_ai_customer_service.admin_backend.services.feishu_integration import (
            dispatch_handoff_case_to_feishu,
        )

        return dispatch_handoff_case_to_feishu(case)
    except Exception as exc:  # noqa: BLE001 - local handoff case must survive notification failures.
        return {
            "enabled": True,
            "ok": False,
            "status": "dispatch_error",
            "adapter": "feishu",
            "case_id": case.get("case_id"),
            "error": repr(exc),
        }


def create_handoff_case(config: dict[str, Any], alert: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("handoff", {}) or {}
    if settings.get("case_store_enabled", True) is False:
        return {"enabled": False}
    try:
        from apps.wechat_ai_customer_service.admin_backend.services.handoff_store import HandoffStore

        case = HandoffStore().create_case(
            {
                "target": alert.get("target"),
                "reason": alert.get("reason"),
                "message_ids": alert.get("message_ids", []),
                "message_contents": alert.get("message_contents", []),
                "reply_text": alert.get("reply_text") or "",
                "operator_alert": alert,
                "product_context": alert.get("product_knowledge", {}) or {},
                "status": "open",
                "priority": 1,
            }
        )
        dispatch = dispatch_handoff_case_notification(case)
        operator_alert = case.get("operator_alert") if isinstance(case.get("operator_alert"), dict) else {}
        operator_alert["dispatch"] = dispatch
        case["operator_alert"] = operator_alert
        return {
            "enabled": True,
            "ok": True,
            "case_id": case.get("case_id"),
            "status": case.get("status"),
            "deduped": bool(case.get("deduped")),
            "dispatch": dispatch,
        }
    except Exception as exc:
        return {"enabled": True, "ok": False, "error": repr(exc)}


def maybe_capture_customer_data(
    config: dict[str, Any],
    target_state: dict[str, Any],
    target: TargetConfig,
    batch: list[dict[str, Any]],
    combined: str,
    write_data: bool,
    intent_result: IntentRouteResult | None = None,
) -> dict[str, Any]:
    settings = config.get("data_capture", {}) or {}
    if not settings.get("enabled", False):
        return {"enabled": False}

    pending = get_open_pending_customer_data(target_state)
    # Auto-close stale pending (older than 120s) so normal conversation can resume
    if pending:
        updated_at_str = str(pending.get("updated_at") or "")
        try:
            updated_at = datetime.fromisoformat(updated_at_str)
            if (datetime.now() - updated_at).total_seconds() > 120:
                close_pending_customer_data(target_state, {
                    "fields": pending.get("fields", {}),
                    "message_ids": pending.get("message_ids", []),
                    "write_result": {"ok": False, "reason": "pending_expired"},
                })
                pending = None
        except Exception:
            pass

    pending_raw_text = str(pending.get("raw_text") or "") if pending else ""
    pending_message_ids = [str(item) for item in pending.get("message_ids", [])] if pending else []
    current_message_ids = [reply_input_message_identity(item) for item in batch if reply_input_message_identity(item)]

    # If current message itself has no customer-data signal, don't merge with stale pending
    from customer_data_capture import has_customer_data_signal
    current_has_signal = has_customer_data_signal(combined, {})
    if pending and not current_has_signal:
        merged_text = combined
        merged_message_ids = current_message_ids
    else:
        merged_text = "\n".join(item for item in [pending_raw_text, combined] if item.strip())
        merged_message_ids = unique_list([*pending_message_ids, *current_message_ids])

    required_fields = [str(item) for item in settings.get("required_fields", ["name", "phone"])]
    extraction = extract_customer_data(merged_text, required_fields=required_fields)

    # Intent is useful as a weak gate, but it must not block explicit lead
    # fields.  Live sales chats often provide "我叫X，电话..." inside an
    # appointment sentence; keyword intent may classify that as appointment or
    # unclear, while the extracted name/phone are still a strong data signal.
    if intent_result is not None:
        strong_field_signal = bool(extraction.fields) and bool(extraction.is_customer_data)
        is_customer_data = bool(extraction.fields) and (
            intent_result.intent == "customer_data_provide" or strong_field_signal
        )
    else:
        is_customer_data = extraction.is_customer_data

    result: dict[str, Any] = {
        "enabled": True,
        "is_customer_data": is_customer_data,
        "complete": extraction.complete,
        "fields": extraction.fields,
        "missing_required_fields": extraction.missing_required_fields,
        "missing_required_labels": missing_field_labels(extraction.missing_required_fields),
        "pending_before": copy.deepcopy(pending),
        "message_ids": merged_message_ids,
        "raw_text": merged_text,
        "write_requested": write_data,
    }
    if not is_customer_data:
        return result
    if not extraction.complete:
        result["write_skipped_reason"] = "missing_required_fields"
        return result
    if not write_data:
        result["write_skipped_reason"] = "write_data_not_requested"
        return result

    workbook_path = resolve_path(settings.get("workbook_path"))
    sheet_name = str(settings.get("sheet_name") or "客户线索")
    result["write_result"] = append_customer_row(
        workbook_path=workbook_path,
        sheet_name=sheet_name,
        source_target=target.name,
        message_ids=merged_message_ids,
        raw_text=merged_text,
        fields=extraction.fields,
    )
    return result


def write_customer_data_if_ready(
    config: dict[str, Any],
    target: TargetConfig,
    data_capture: dict[str, Any],
) -> None:
    if not data_capture.get("enabled") or not data_capture.get("is_customer_data"):
        return
    if not data_capture.get("complete"):
        return
    if data_capture.get("write_result", {}).get("ok"):
        return

    settings = config.get("data_capture", {}) or {}
    workbook_path = resolve_path(settings.get("workbook_path"))
    sheet_name = str(settings.get("sheet_name") or "客户线索")
    data_capture["write_requested"] = True
    data_capture.pop("write_skipped_reason", None)
    data_capture["write_result"] = append_customer_row(
        workbook_path=workbook_path,
        sheet_name=sheet_name,
        source_target=target.name,
        message_ids=[str(item) for item in data_capture.get("message_ids", [])],
        raw_text=str(data_capture.get("raw_text") or ""),
        fields=data_capture.get("fields", {}) or {},
    )


def decide_reply_with_data_capture(
    combined: str,
    rules: dict[str, Any],
    config: dict[str, Any],
    data_capture: dict[str, Any],
    product_knowledge: dict[str, Any] | None = None,
) -> ReplyDecision:
    if data_capture.get("is_customer_data"):
        if data_capture.get("complete"):
            reply = data_capture_reply(config, data_capture, complete=True)
            needs_handoff = visible_platform_terms_hit(
                combined,
                settings=config.get("llm_reply_synthesis", {}) or {},
            ) and not customer_data_complete_can_auto_ack(combined)
            return ReplyDecision(
                reply_text=reply,
                rule_name="customer_data_capture",
                matched=True,
                need_handoff=needs_handoff,
                reason="customer_data_complete_requires_handoff" if needs_handoff else "customer_data_complete",
            )
        reply = data_capture_reply(config, data_capture, complete=False)
        return ReplyDecision(
            reply_text=reply,
            rule_name="customer_data_incomplete",
            matched=True,
            need_handoff=False,
            reason="customer_data_missing_required_fields",
            )
    if product_knowledge and product_knowledge.get("matched") and product_knowledge.get("reply_text"):
        return ReplyDecision(
            reply_text=str(product_knowledge.get("reply_text") or ""),
            rule_name="product_knowledge",
            matched=True,
            need_handoff=bool(product_knowledge.get("needs_handoff")),
            reason=str(product_knowledge.get("reason") or "product_knowledge_matched"),
        )
    return decide_reply(combined, rules)


def maybe_match_product_knowledge(
    config: dict[str, Any],
    target_state: dict[str, Any],
    combined: str,
    data_capture: dict[str, Any],
) -> dict[str, Any]:
    settings = config.get("product_knowledge", {}) or {}
    if not settings.get("enabled", False):
        return {"enabled": False}
    if data_capture.get("is_customer_data"):
        return {"enabled": True, "matched": False, "reason": "skipped_for_customer_data"}
    path = resolve_path(settings.get("path"))
    knowledge = load_product_knowledge(path)
    result = decide_product_knowledge_reply(
        combined,
        knowledge,
        context=target_state.get("conversation_context", {}) or {},
        matching_settings=product_entity_resolution_settings(config),
    )
    result["path"] = str(path)
    return result


def apply_local_customer_service_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Overlay Local Console switches onto the workflow config used by the listener."""
    merged = copy.deepcopy(config)
    try:
        settings = CustomerServiceSettings(tenant_id=active_tenant_id()).get()
    except Exception:
        return merged
    merged["_local_customer_service_settings"] = settings
    managed_session_targets = bool(settings.get("session_targets_managed", False))
    respond_all_unread_sessions = bool(settings.get("respond_all_unread_sessions", False))
    configured_targets = [item for item in merged.get("targets", []) or [] if isinstance(item, dict)]
    configured_by_name = {str(item.get("name") or "").strip(): item for item in configured_targets if str(item.get("name") or "").strip()}
    managed_targets = [item for item in settings.get("session_targets", []) or [] if isinstance(item, dict)]
    if managed_session_targets:
        enabled_targets: list[dict[str, Any]] = []
        ignored_names: list[str] = []
        for raw in managed_targets:
            name = str(raw.get("name") or raw.get("target_name") or "").strip()
            if not name:
                continue
            base = configured_by_name.get(name, {})
            enabled = bool(raw.get("enabled", False))
            exact = bool(raw.get("exact", base.get("exact", True)))
            allow_self = bool(base.get("allow_self_for_test", False))
            if allow_self and name != FILE_TRANSFER_ASSISTANT:
                allow_self = False
            try:
                max_batch = int(
                    raw.get("max_batch_messages", base.get("max_batch_messages", default_max_batch_messages(merged)))
                    or default_max_batch_messages(merged)
                )
            except (TypeError, ValueError):
                max_batch = default_max_batch_messages(merged)
            if enabled:
                enabled_targets.append(
                    {
                        "name": name,
                        "enabled": True,
                        "exact": exact,
                        "allow_self_for_test": allow_self,
                        "max_batch_messages": max(1, max_batch),
                    }
                )
            else:
                ignored_names.append(name)
        merged["targets"] = enabled_targets
        merged["_local_customer_service_session_routing"] = {
            "managed": True,
            "respond_all_unread_sessions": respond_all_unread_sessions,
            "ignored_names": ignored_names,
            "enabled_names": [str(item.get("name") or "") for item in enabled_targets if str(item.get("name") or "")],
        }
    else:
        merged["_local_customer_service_session_routing"] = {
            "managed": False,
            "respond_all_unread_sessions": respond_all_unread_sessions,
            "ignored_names": [],
            "enabled_names": [str(item.get("name") or "").strip() for item in configured_targets if str(item.get("name") or "").strip()],
        }
    if respond_all_unread_sessions:
        merged.setdefault("multi_target", {})
        if isinstance(merged.get("multi_target"), dict):
            merged["multi_target"]["enabled"] = True
        routing = merged.get("_local_customer_service_session_routing", {})
        if isinstance(routing, dict):
            enabled_names = {str(item).strip() for item in routing.get("enabled_names", []) or [] if str(item).strip()}
            ignored_names = {
                str(item).strip()
                for item in routing.get("ignored_names", []) or []
                if str(item).strip()
            }
            if FILE_TRANSFER_ASSISTANT not in enabled_names:
                ignored_names.add(FILE_TRANSFER_ASSISTANT)
            routing["ignored_names"] = sorted(ignored_names)
            merged["_local_customer_service_session_routing"] = routing
    use_llm = settings.get("use_llm", True) is not False
    raw_messages = dict(merged.get("raw_messages", {}) or {})
    raw_messages["enabled"] = settings.get("record_messages", True) is not False
    raw_messages["auto_learn"] = settings.get("auto_learn", True) is not False
    raw_messages["use_llm"] = use_llm
    merged["raw_messages"] = raw_messages

    def normalize_llm_module_settings(
        raw_settings: dict[str, Any] | None,
        *,
        enabled: bool,
        default_tier: str = "flash",
        normalize_model_routing: bool = False,
        manual_candidate_keys: tuple[str, ...] = ("candidate_json_path",),
    ) -> dict[str, Any]:
        module = dict(raw_settings or {})
        if not enabled:
            return module
        provider_text = str(module.get("provider") or "manual_json").strip()
        has_manual_candidate = any(str(module.get(key) or "").strip() for key in manual_candidate_keys)
        if provider_text.lower() == "manual_json" and not has_manual_candidate and isinstance(module.get("candidate"), dict):
            has_manual_candidate = True
        if provider_text.lower() == "manual_json" and has_manual_candidate:
            return module

        active_provider = configured_llm_provider()
        # Customer-service LLM modules follow the global route selected in the
        # console.  Old example/runtime configs may still contain provider-
        # scoped model names; do not let those stale values override the
        # operator's current model switch.
        effective_provider = resolve_effective_llm_provider(active_provider or provider_text)
        follow_global_route = bool(active_provider)
        module["provider"] = effective_provider
        module["base_url"] = resolve_llm_base_url(
            provider=effective_provider,
            explicit_base_url="" if follow_global_route else str(module.get("base_url") or ""),
        )
        model_tier = str(module.get("model_tier") or default_tier or "flash").strip() or "flash"
        module["model"] = resolve_llm_tier_model(
            provider=effective_provider,
            tier=model_tier,
            explicit_model="" if follow_global_route else str(module.get("model") or ""),
        )
        if normalize_model_routing:
            routing = dict(module.get("model_routing", {}) or {})
            routing["flash_model"] = resolve_llm_tier_model(
                provider=effective_provider,
                tier="flash",
                explicit_model="" if follow_global_route else str(routing.get("flash_model") or module.get("model") or ""),
            )
            routing["pro_model"] = resolve_llm_tier_model(
                provider=effective_provider,
                tier="pro",
                explicit_model="" if follow_global_route else str(routing.get("pro_model") or module.get("model") or ""),
            )
            module["model_routing"] = routing
        return module

    intent_assist = dict(merged.get("intent_assist", {}) or {})
    intent_assist["enabled"] = use_llm
    intent_assist["mode"] = str(intent_assist.get("mode") or "heuristic")
    llm_advisory = normalize_llm_module_settings(
        intent_assist.get("llm_advisory", {}) or {},
        enabled=use_llm,
    )
    llm_advisory["enabled"] = use_llm
    llm_advisory.setdefault("advisory_only", True)
    llm_advisory.setdefault("apply_to_reply", False)
    intent_assist["llm_advisory"] = llm_advisory
    merged["intent_assist"] = intent_assist

    rag_response = dict(merged.get("rag_response", {}) or {})
    rag_response["enabled"] = settings.get("rag_enabled", True) is not False
    merged["rag_response"] = rag_response

    style_adapter = dict(merged.get("reply_style_adapter", {}) or {})
    style_adapter["enabled"] = settings.get("style_adapter_enabled", True) is not False
    style_adapter.setdefault("mode", "fast_local")
    style_adapter.setdefault("identity_guard_aware", True)
    merged["reply_style_adapter"] = style_adapter

    llm_synthesis = normalize_llm_module_settings(
        merged.get("llm_reply_synthesis", {}) or {},
        enabled=use_llm,
        normalize_model_routing=True,
        manual_candidate_keys=("candidate_json_path",),
    )
    llm_synthesis["enabled"] = use_llm and settings.get("llm_reply_synthesis_enabled", True) is not False
    llm_synthesis["identity_guard_enabled"] = settings.get("identity_guard_enabled", True) is not False
    llm_synthesis.setdefault("mode", "guarded_auto")
    llm_synthesis.setdefault("require_evidence", True)
    llm_synthesis.setdefault("require_structured_for_authority", True)
    llm_synthesis.setdefault("fallback_to_existing_reply", True)
    merged["llm_reply_synthesis"] = llm_synthesis

    brain_mode = normalize_customer_service_brain_mode(settings.get("customer_service_brain_mode"))
    customer_service_brain = normalize_llm_module_settings(
        merged.get("customer_service_brain", {}) or {},
        enabled=use_llm and brain_mode == "brain_first",
        default_tier="pro",
    )
    customer_service_brain["enabled"] = use_llm and brain_mode == "brain_first"
    customer_service_brain["mode"] = brain_mode
    customer_service_brain.setdefault("model_tier", "pro")
    customer_service_brain.setdefault("timeout_seconds", 18)
    customer_service_brain.setdefault("max_tokens", 2600)
    customer_service_brain.setdefault("temperature", 0.35)
    customer_service_brain.setdefault("max_reply_segments", 3)
    customer_service_brain.setdefault("require_fact_claims", True)
    customer_service_brain.setdefault("require_final_visible_polish", True)
    customer_service_brain.setdefault("fallback_to_legacy_on_error", False)
    customer_service_brain.setdefault("blocking_shadow_enabled", False)
    customer_service_brain.setdefault("history_char_budget", 1200)
    customer_service_brain.setdefault("summary_char_budget", 280)
    customer_service_brain.setdefault("current_batch_char_budget", 420)
    customer_service_brain.setdefault("max_prompt_product_items", 5)
    customer_service_brain.setdefault("max_prompt_formal_items", 3)
    customer_service_brain.setdefault("max_prompt_style_examples", 1)
    customer_service_brain.setdefault("max_prompt_rag_hits", 1)
    customer_service_brain.setdefault("prompt_item_text_chars", 220)
    merged["customer_service_brain"] = customer_service_brain

    product_entity_resolution = normalize_llm_module_settings(
        merged.get("product_entity_resolution", {}) or {},
        enabled=use_llm and (merged.get("product_entity_resolution", {}) or {}).get("enabled", True) is not False,
        default_tier="flash",
    )
    product_entity_resolution["enabled"] = use_llm and product_entity_resolution.get("enabled", True) is not False
    product_entity_resolution.setdefault("model_tier", "flash")
    product_entity_resolution.setdefault("timeout_seconds", 3)
    product_entity_resolution.setdefault("max_candidates", 12)
    product_entity_resolution.setdefault("min_confidence", 0.72)
    product_entity_resolution.setdefault("temperature", 0.0)
    product_entity_resolution.setdefault("max_tokens", 240)
    merged["product_entity_resolution"] = product_entity_resolution

    final_polish = normalize_llm_module_settings(
        merged.get("final_visible_llm_polish", {}) or {},
        enabled=use_llm,
        default_tier="flash",
    )
    final_polish["enabled"] = use_llm and settings.get("final_visible_llm_polish_enabled", True) is not False
    final_polish["identity_guard_enabled"] = settings.get("identity_guard_enabled", True) is not False
    final_polish.setdefault("required_for_send", True)
    final_polish.setdefault("model_tier", "flash")
    final_polish.setdefault("max_tokens", 260)
    final_polish.setdefault("timeout_seconds", 4)
    final_polish.setdefault("retry_count", 0)
    final_polish.setdefault("temperature", 0.45)
    final_polish.setdefault("max_reply_chars", 620)
    final_polish.setdefault("allow_send_when_unavailable", True)
    final_polish.setdefault("skip_short_reply_fast_path_enabled", False)
    final_polish.setdefault("skip_short_reply_max_chars", 46)
    final_polish.setdefault("skip_short_reply_max_sentences", 2)
    merged["final_visible_llm_polish"] = final_polish

    reply_multi_bubble = dict(merged.get("reply_multi_bubble", {}) or {})
    reply_multi_bubble.setdefault("enabled", True)
    reply_multi_bubble.setdefault("min_split_chars", 82)
    reply_multi_bubble.setdefault("max_segments", 3)
    reply_multi_bubble.setdefault("preferred_segment_chars", 54)
    reply_multi_bubble.setdefault("max_segment_chars", 84)
    reply_multi_bubble.setdefault("min_segment_chars", 22)
    reply_multi_bubble.setdefault("three_segment_threshold_chars", 166)
    reply_multi_bubble.setdefault("inter_segment_delay_min_ms", 180)
    reply_multi_bubble.setdefault("inter_segment_delay_max_ms", 420)
    reply_multi_bubble.setdefault("verify_each_segment", False)
    reply_multi_bubble.setdefault("retry_on_transient_send_failures", True)
    reply_multi_bubble.setdefault("max_transient_retry_per_segment", 1)
    reply_multi_bubble.setdefault("transient_retry_delay_min_ms", 600)
    reply_multi_bubble.setdefault("transient_retry_delay_max_ms", 1200)
    merged["reply_multi_bubble"] = reply_multi_bubble

    scheduler_freshness = dict(merged.get("scheduler_freshness", {}) or {})
    scheduler_freshness.setdefault("enabled", True)
    scheduler_freshness.setdefault("mode", "preview_first")
    scheduler_freshness.setdefault("strict_check_interval_seconds", 75)
    scheduler_freshness.setdefault("strict_check_after_llm_seconds", 45)
    merged["scheduler_freshness"] = scheduler_freshness

    data_capture = dict(merged.get("data_capture", {}) or {})
    data_capture["enabled"] = settings.get("data_capture_enabled", True) is not False
    merged["data_capture"] = data_capture

    handoff = dict(merged.get("handoff", {}) or {})
    handoff["enabled"] = settings.get("handoff_enabled", True) is not False
    merged["handoff"] = handoff

    operator_alert = dict(merged.get("operator_alert", {}) or {})
    operator_alert["enabled"] = settings.get("operator_alert_enabled", True) is not False
    merged["operator_alert"] = operator_alert
    merged = apply_customer_service_brain_startup_guard(merged, settings=settings)
    return apply_customer_service_live_safety_guard(merged, settings=settings)


def brain_startup_guard_required(config: dict[str, Any] | None) -> bool:
    cfg = config if isinstance(config, dict) else {}
    guard = cfg.get("customer_service_brain_startup_guard")
    if isinstance(guard, dict) and guard.get("enabled") is not None:
        return str(guard.get("enabled")).strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(cfg.get("_require_customer_service_brain_first_startup_guard"))


def evaluate_customer_service_brain_startup_guard(
    config: dict[str, Any] | None,
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = config if isinstance(config, dict) else {}
    if not brain_startup_guard_required(cfg):
        return {"enabled": False, "ok": True, "fail_reasons": []}
    brain = cfg.get("customer_service_brain") if isinstance(cfg.get("customer_service_brain"), dict) else {}
    final_polish = cfg.get("final_visible_llm_polish") if isinstance(cfg.get("final_visible_llm_polish"), dict) else {}
    local = settings if isinstance(settings, dict) else {}
    fail_reasons: list[str] = []
    if local.get("use_llm", True) is False:
        fail_reasons.append("llm_disabled")
    if brain.get("enabled") is not True:
        fail_reasons.append("brain_disabled")
    if str(brain.get("mode") or "").strip() != "brain_first":
        fail_reasons.append("brain_mode_not_brain_first")
    if brain.get("fallback_to_legacy_on_error", False) is not False:
        fail_reasons.append("legacy_fallback_enabled")
    if final_polish.get("enabled") is not True:
        fail_reasons.append("final_polish_disabled")
    if final_polish.get("required_for_send", True) is not True:
        fail_reasons.append("final_polish_not_required")
    return {
        "enabled": True,
        "ok": not fail_reasons,
        "fail_reasons": fail_reasons,
        "tenant_id": active_tenant_id(),
        "brain_enabled": bool(brain.get("enabled")),
        "brain_mode": str(brain.get("mode") or ""),
        "fallback_to_legacy_on_error": bool(brain.get("fallback_to_legacy_on_error", False)),
        "final_polish_enabled": bool(final_polish.get("enabled")),
        "final_polish_required": bool(final_polish.get("required_for_send", True)),
    }


def apply_customer_service_brain_startup_guard(config: dict[str, Any], *, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = evaluate_customer_service_brain_startup_guard(config, settings=settings)
    merged = dict(config)
    merged["_customer_service_brain_startup_guard"] = summary
    if summary.get("enabled") and not summary.get("ok"):
        raise CustomerServiceBrainStartupError(summary)
    return merged


def update_conversation_context(target_state: dict[str, Any], product_knowledge: dict[str, Any] | None) -> None:
    if not product_knowledge:
        return
    if not product_knowledge.get("matched"):
        return
    if product_knowledge.get("match_type") != "product":
        return
    context = dict(target_state.get("conversation_context", {}) or {})
    context["last_product_id"] = product_knowledge.get("product_id")
    context["last_product_name"] = product_knowledge.get("product_name")
    context["last_product_unit"] = product_knowledge.get("product_unit")
    if product_knowledge.get("quantity") not in (None, ""):
        context["last_quantity"] = product_knowledge.get("quantity")
    if product_knowledge.get("shipping_city"):
        context["last_shipping_city"] = product_knowledge.get("shipping_city")
    if product_knowledge.get("unit_price") not in (None, ""):
        context["last_unit_price"] = product_knowledge.get("unit_price")
    if product_knowledge.get("total") not in (None, ""):
        context["last_total"] = product_knowledge.get("total")
    context["updated_at"] = datetime.now().isoformat(timespec="seconds")
    target_state["conversation_context"] = context


def update_conversation_context_from_reply_event(target_state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    product_ids = select_product_ids_from_reply_event(event)
    if not product_ids:
        return {}
    product_context: dict[str, Any] = {}
    for product_id in product_ids:
        product_context = load_product_context_by_id(product_id)
        if product_context:
            break
    context = dict(target_state.get("conversation_context", {}) or {})
    if product_context:
        context.update(product_context)
    context["recent_product_ids"] = product_ids[:5]
    context["recent_product_updated_at"] = datetime.now().isoformat(timespec="seconds")
    context["updated_at"] = datetime.now().isoformat(timespec="seconds")
    target_state["conversation_context"] = context
    update = dict(product_context)
    update["recent_product_ids"] = product_ids[:5]
    return update


def update_conversation_preference_context(target_state: dict[str, Any], customer_text: str) -> dict[str, Any]:
    preferences = extract_stable_customer_preference_terms(customer_text)
    if not preferences:
        return {}
    context = dict(target_state.get("conversation_context", {}) or {})
    previous_need_text = str(context.get("last_customer_need_text") or "").strip()
    previous_terms = [str(item).strip() for item in (context.get("last_customer_need_terms") or []) if str(item).strip()]
    need_text = str(customer_text or "").strip()
    if should_merge_followup_preference_context(
        current_text=need_text,
        current_terms=preferences,
        previous_text=previous_need_text,
        previous_terms=previous_terms,
    ):
        need_text = f"{previous_need_text}；{need_text}"
        preferences = merge_preference_terms(previous_terms, preferences)
    context["last_customer_need_text"] = truncate_context_text(need_text, 240)
    context["last_customer_need_terms"] = preferences
    context["last_customer_need_updated_at"] = datetime.now().isoformat(timespec="seconds")
    target_state["conversation_context"] = context
    return {
        "last_customer_need_text": context["last_customer_need_text"],
        "last_customer_need_terms": preferences,
    }


def should_merge_followup_preference_context(
    *,
    current_text: str,
    current_terms: list[str],
    previous_text: str,
    previous_terms: list[str],
) -> bool:
    """Keep earlier budget/use-case constraints when a later turn refines the same need."""

    if not previous_text or not previous_terms or not current_terms:
        return False
    if preference_text_has_budget(current_text):
        return False
    if not preference_text_has_budget(previous_text) and "预算" not in previous_terms:
        return False
    compact = re.sub(r"\s+", "", str(current_text or "").lower())
    continuation_terms = (
        "推荐",
        "挑",
        "选",
        "两台",
        "两个",
        "几台",
        "几个",
        "哪台",
        "哪个",
        "先看",
        "直接",
        "靠谱",
        "省油",
        "油耗",
        "能看",
        "可看",
        "现车",
        "南京",
    )
    return any(term in compact for term in continuation_terms)


def preference_text_has_budget(text: str) -> bool:
    raw = str(text or "")
    if re.search(r"\d+(?:\.\d+)?\s*(?:万|w|W)", raw):
        return True
    return bool(re.search(r"[零〇一二两三四五六七八九十百]+(?:点[零〇一二两三四五六七八九]+)?\s*万", raw))


def merge_preference_terms(previous_terms: list[str], current_terms: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for term in [*previous_terms, *current_terms]:
        clean = str(term or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        merged.append(clean)
    return merged[:10]


def extract_stable_customer_preference_terms(text: str) -> list[str]:
    raw = str(text or "")
    compact = re.sub(r"\s+", "", raw.lower())
    if not compact:
        return []
    terms: list[str] = []
    stable_terms = (
        "mpv",
        "商务车",
        "七座",
        "7座",
        "家用",
        "一家人",
        "全家",
        "老婆",
        "爱人",
        "媳妇",
        "女士",
        "接娃",
        "接送",
        "孩子",
        "买菜",
        "suv",
        "轿车",
        "代步",
        "通勤",
        "省油",
        "油耗",
        "自动挡",
        "倒车影像",
        "倒车",
        "影像",
        "雷达",
        "好停",
        "好开",
        "新手",
        "新能源",
        "混动",
        "豪华",
        "面子",
        "练手",
        "车况透明",
        "费油",
        "靠谱",
        "能看",
        "可看",
        "现车",
        "本地",
    )
    for term in stable_terms:
        if term.lower() in compact:
            terms.append(term)
    if preference_text_has_budget(raw):
        terms.append("预算")
    normalized: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        normalized.append(term)
    return normalized[:8]


def truncate_context_text(text: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit)].rstrip()


def select_single_product_id_from_reply_event(event: dict[str, Any]) -> str:
    normalized = select_product_ids_from_reply_event(event)
    return normalized[0] if len(normalized) == 1 else ""


def select_product_ids_from_reply_event(event: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    product_knowledge = event.get("product_knowledge") if isinstance(event.get("product_knowledge"), dict) else {}
    if (
        product_knowledge
        and product_knowledge.get("matched")
        and str(product_knowledge.get("match_type") or "") == "product"
        and str(product_knowledge.get("product_id") or "").strip()
    ):
        ids.append(str(product_knowledge.get("product_id") or "").strip())
    visible_ids: list[str] = []
    for text in visible_reply_texts_from_event(event):
        visible_ids.extend(product_ids_mentioned_in_visible_reply(text))
    if visible_ids:
        ids.extend(visible_ids)
    else:
        synthesis = event.get("llm_reply_synthesis") if isinstance(event.get("llm_reply_synthesis"), dict) else {}
        ids.extend(product_ids_from_synthesis_payload(synthesis))
        brain = event.get("customer_service_brain") if isinstance(event.get("customer_service_brain"), dict) else {}
        if brain_payload_allows_context_update(brain):
            ids.extend(product_ids_from_synthesis_payload(brain))
    normalized: list[str] = []
    seen: set[str] = set()
    for item in ids:
        product_id = str(item or "").strip()
        if not product_id or product_id in seen:
            continue
        seen.add(product_id)
        normalized.append(product_id)
    return normalized


def brain_payload_allows_context_update(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    rule_name = str(payload.get("rule_name") or "").strip()
    if rule_name in {"customer_service_brain_safe_fallback", "customer_service_brain_no_visible_reply"}:
        return False
    if not bool(payload.get("applied")):
        return False
    if not rule_name.startswith("customer_service_brain_"):
        return False
    if str(payload.get("reason") or "") in {
        "brain_plan_validation_failed",
        "brain_quality_verification_failed",
        "brain_guard_rejected",
    }:
        return False
    return True


def visible_reply_texts_from_event(event: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for key in ("reply_text", "raw_reply_text"):
        value = str(event.get(key) or "").strip()
        if value:
            texts.append(value)
    decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
    for key in ("reply_text", "raw_reply_text"):
        value = str(decision.get(key) or "").strip()
        if value:
            texts.append(value)
    brain = event.get("customer_service_brain") if isinstance(event.get("customer_service_brain"), dict) else {}
    for key in ("reply_text", "raw_reply_text"):
        value = str(brain.get(key) or "").strip()
        if value:
            texts.append(value)
    normalized: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def product_ids_mentioned_in_visible_reply(text: str) -> list[str]:
    clean = str(text or "").strip()
    if not clean:
        return []
    try:
        from knowledge_runtime import KnowledgeRuntime

        items = KnowledgeRuntime().list_items("products")
    except Exception:
        return []
    scored: list[tuple[int, int, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        if not isinstance(data, dict):
            continue
        product_id = str(item.get("id") or data.get("id") or data.get("product_id") or "").strip()
        if not product_id:
            continue
        matched_aliases = collect_catalog_product_aliases(data, clean)
        concrete_aliases = [alias for alias in matched_aliases if is_strong_visible_product_alias(alias)]
        if not concrete_aliases:
            continue
        first_index = min(
            (clean.find(alias) for alias in concrete_aliases if alias and clean.find(alias) >= 0),
            default=10**9,
        )
        preference_rank = visible_reply_product_preference_rank(clean, concrete_aliases, first_index)
        scored.append((preference_rank, first_index, product_id))
    scored.sort(key=lambda item: (item[0], item[1]))
    result: list[str] = []
    seen: set[str] = set()
    for _, _, product_id in scored:
        if product_id in seen:
            continue
        seen.add(product_id)
        result.append(product_id)
        if len(result) >= 5:
            break
    return result


def brain_first_low_authority_fast_plan_precheck(
    *,
    config: dict[str, Any],
    combined: str,
    batch: list[dict[str, Any]],
    target_state: dict[str, Any],
) -> dict[str, Any]:
    """Detect low-authority short turns that can skip legacy pre-Brain work."""

    settings = effective_brain_settings(config)
    if not bool(settings.get("enabled")) or str(settings.get("mode") or "") != "brain_first":
        return {"enabled": False, "reason": "brain_first_not_enabled"}
    decision = low_authority_fast_profile_decision(
        settings=settings,
        combined=combined,
        batch=batch,
        target_state=target_state,
    )
    if not bool(decision.get("enabled")):
        return {"enabled": False, "reason": str(decision.get("reason") or "not_low_authority_fast"), "profile": decision}
    return {"enabled": True, "reason": "brain_first_low_authority_fast_precheck", "profile": decision}


def visible_reply_product_preference_rank(clean: str, aliases: list[str], first_index: int) -> int:
    """Prefer the product explicitly selected by the visible reply, not just the first mention."""
    if first_index >= 10**9:
        return 20
    primary_markers_before = (
        "更偏",
        "更倾向",
        "主推",
        "首推",
        "首选",
        "第一顺位",
        "第一推荐",
        "优先推荐",
        "优先看",
        "先看",
        "先排",
        "先放",
        "放前面",
        "排前面",
        "我会看",
        "我会先看",
        "我建议",
        "更建议",
    )
    primary_markers_after = (
        "更合适",
        "更适合",
        "更贴",
        "更贴合",
        "更靠谱",
        "更稳",
        "更推荐",
        "优先",
        "主推",
        "首选",
        "排前面",
        "放前面",
        "第一顺位",
    )
    backup_markers = (
        "备选",
        "放第二",
        "第二顺位",
        "第二个看",
        "排第二",
        "再看",
        "后面看",
    )
    compact_clean = re.sub(r"\s+", "", clean)
    for alias in sorted((str(item or "").strip() for item in aliases), key=len, reverse=True):
        if not alias:
            continue
        compact_alias = re.sub(r"\s+", "", alias)
        if not compact_alias:
            continue
        for match in re.finditer(re.escape(compact_alias), compact_clean, re.IGNORECASE):
            start, end = match.span()
            before = compact_clean[max(0, start - 18) : start]
            after = compact_clean[end : min(len(compact_clean), end + 18)]
            if any(marker in before for marker in primary_markers_before):
                return 0
            if any(marker in after for marker in primary_markers_after):
                return 0
            if any(marker in before or marker in after for marker in backup_markers):
                return 40
    return 20


def is_strong_visible_product_alias(alias: str) -> bool:
    text = re.sub(r"\s+", "", str(alias or "")).strip()
    if not text:
        return False
    compact = text.lower()
    weak = {re.sub(r"\s+", "", item).lower() for item in WEAK_VISIBLE_PRODUCT_ALIAS_TERMS}
    if compact in weak:
        return False
    return is_concrete_catalog_alias(alias)


def product_ids_from_synthesis_payload(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return []
    ids: list[str] = []
    summary = payload.get("evidence_summary") if isinstance(payload.get("evidence_summary"), dict) else {}
    primary_ids: list[str] = []
    for item in summary.get("product_ids", []) or []:
        if str(item or "").strip():
            primary_ids.append(str(item).strip())
    unique_primary = []
    seen_primary: set[str] = set()
    for item in primary_ids:
        if item in seen_primary:
            continue
        seen_primary.add(item)
        unique_primary.append(item)
    if len(unique_primary) == 1:
        return unique_primary
    for item in summary.get("evidence_ids", []) or []:
        product_id = parse_product_id_marker(item)
        if product_id:
            ids.append(product_id)
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    for item in candidate.get("used_evidence", []) or []:
        product_id = parse_product_id_marker(item)
        if product_id:
            ids.append(product_id)
    authority_sources = payload.get("authority_sources") if isinstance(payload.get("authority_sources"), dict) else {}
    for item in authority_sources.get("product_master", []) or []:
        if str(item or "").strip():
            ids.append(str(item).strip())
    return ids


def parse_product_id_marker(value: Any) -> str:
    text = str(value or "").strip()
    for prefix in ("product:", "catalog_product:", "product_master:"):
        if text.startswith(prefix):
            return text.split(":", 1)[1].strip()
    return ""


def load_product_context_by_id(product_id: str) -> dict[str, Any]:
    try:
        from knowledge_runtime import KnowledgeRuntime

        item = KnowledgeRuntime().get_item("products", product_id)
    except Exception:
        return {}
    if not isinstance(item, dict):
        return {}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    name = str(data.get("name") or item.get("name") or "").strip()
    unit = str(data.get("unit") or item.get("unit") or "").strip()
    price = data.get("price", item.get("price"))
    context = {
        "last_product_id": product_id,
        "last_product_name": name,
        "last_product_unit": unit,
        "last_product_source": "product_master",
    }
    if price not in (None, ""):
        context["last_product_price"] = price
        context["last_unit_price"] = price
    return {key: value for key, value in context.items() if value not in (None, "", [], {})}


def maybe_analyze_intent(
    config: dict[str, Any],
    combined: str,
    decision: ReplyDecision,
    reply_text: str,
    data_capture: dict[str, Any],
    product_knowledge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = config.get("intent_assist", {}) or {}
    if not settings.get("enabled", False):
        return {"enabled": False}
    mode = str(settings.get("mode") or "heuristic")
    if mode != "heuristic":
        return {
            "enabled": True,
            "mode": mode,
            "ok": False,
            "reason": "unsupported_intent_assist_mode",
        }

    evidence_pack = build_evidence_pack(
        combined,
        context=build_evidence_context_for_pack(
            config,
            conversation_context_from_product_result(product_knowledge or {}),
        ),
    )
    clear_finance_process_handoff_for_intent_evidence(evidence_pack, combined=combined)
    analysis_context = build_intent_context(
        config,
        data_capture,
        decision,
        product_knowledge or {},
        evidence_pack=evidence_pack,
    )
    result = analyze_intent(combined, context=analysis_context)
    payload = {
        **result.__dict__,
        "ok": True,
        "advisory_only": bool(settings.get("advisory_only", True)),
        "evidence": summarize_evidence_pack(evidence_pack),
        "rule_decision": {
            "rule_name": decision.rule_name,
            "matched": decision.matched,
            "reason": decision.reason,
            "reply_text": reply_text,
        },
    }
    safety = payload.get("evidence", {}).get("safety", {}) or {}
    clear_no_relevant_handoff_after_safe_rule_match(safety, decision, combined=combined, settings=config.get("llm_reply_synthesis", {}) or {})
    social_redirect = soft_social_redirect_for_handoff(combined)
    if social_redirect:
        payload["social_offtopic_soft_redirect"] = True
    if social_redirect and isinstance(safety, dict) and safety.get("must_handoff"):
        # Off-topic social small-talk should prefer a friendly soft redirect,
        # instead of inheriting stale business handoff signals from context.
        safety["must_handoff"] = False
        safety["reasons"] = []
    clear_soft_handoff_for_customer_data_incomplete_prompt(
        payload,
        decision=decision,
        data_capture=data_capture,
    )
    if isinstance(safety, dict) and safety.get("must_handoff"):
        reasons = [str(item) for item in safety.get("reasons", []) or [] if str(item)]
        payload["needs_handoff"] = True
        payload["safe_to_auto_send"] = False
        intent_tags = payload.get("evidence", {}).get("intent_tags", []) or []
        payload["recommended_action"] = "handoff_for_approval" if "discount" in intent_tags else "handoff"
        payload["reason"] = "evidence_safety:" + ",".join(reasons) if reasons else "evidence_safety_must_handoff"
    clear_soft_handoff_for_customer_data_complete_ack(
        payload,
        decision=decision,
        data_capture=data_capture,
        combined=combined,
    )
    suggested_reply = str(payload.get("suggested_reply") or "")
    payload["would_change_reply"] = bool(suggested_reply and suggested_reply not in reply_text)

    # Run LLM advisory in background so it never blocks the customer reply path.
    llm_settings = settings.get("llm_advisory", {}) or {}
    if llm_settings.get("enabled") and str(llm_settings.get("provider") or "") == "deepseek":
        def _background_advisory(
            settings_param: dict[str, Any],
            combined_param: str,
            context_param: dict[str, Any],
            heuristic_param: Any,
        ) -> None:
            try:
                adv = build_llm_advisory(
                    settings=settings_param,
                    combined=combined_param,
                    context=context_param,
                    heuristic=heuristic_param,
                )
                log_path = resolve_path("runtime/logs/wechat_customer_service/background_advisory.jsonl")
                append_jsonl(
                    log_path,
                    {
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "combined": combined_param,
                        "advisory": adv,
                    },
                )
            except Exception:
                pass

        threading.Thread(
            target=_background_advisory,
            args=(settings, combined, analysis_context, result),
            daemon=True,
        ).start()
        payload["llm_advisory"] = {
            "enabled": True,
            "provider": "deepseek",
            "status": "background_started",
            "reason": "advisory_executed_in_background",
        }
    else:
        payload["llm_advisory"] = build_llm_advisory(
            settings=settings,
            combined=combined,
            context=analysis_context,
            heuristic=result,
        )
    return payload


def clear_no_relevant_handoff_after_safe_rule_match(
    safety: dict[str, Any],
    decision: ReplyDecision,
    *,
    combined: str,
    settings: dict[str, Any],
) -> None:
    if not isinstance(safety, dict) or not safety.get("must_handoff"):
        return
    reasons = {str(item) for item in safety.get("reasons", []) or [] if str(item)}
    if not reasons or not reasons <= {"no_relevant_business_evidence", "auto_reply_disabled"}:
        return
    if not decision.matched or decision.need_handoff or not str(decision.reply_text or "").strip():
        return
    if visible_platform_terms_hit(combined, settings=settings):
        return
    safety["must_handoff"] = False
    safety["allowed_auto_reply"] = True
    safety["reasons"] = []
    safety["configured_rule_match_override"] = True


def visible_platform_terms_hit(text: str, *, settings: dict[str, Any]) -> bool:
    platform_rules = load_platform_safety_rules(settings).get("item", {})
    clean = "".join(str(text or "").split())
    groups = (
        "forbidden_reply_terms",
        "appointment_commitment_terms",
        "commitment_terms",
    )
    return any(term and term in clean for group in groups for term in guard_term_set(platform_rules, group))


def customer_data_complete_can_auto_ack(text: str) -> bool:
    clean = normalize_reply_content_key(text)
    if not clean:
        return False
    if not any(term in clean for term in ("电话", "手机号", "联系方式", "我叫", "姓名", "先生", "女士")):
        return False
    if not any(term in clean for term in ("到店", "看车", "过去", "来店", "周一", "周二", "周三", "周四", "周五", "周六", "周日", "上午", "下午", "晚上")):
        return False
    hard_terms = (
        "定金",
        "订金",
        "留车",
        "锁车",
        "当天提",
        "直接提",
        "提车",
        "最低",
        "底价",
        "优惠",
        "包过",
        "保证",
        "置换价",
        "抵多少",
        "估价",
        "合同",
        "发票",
        "身份证",
        "银行卡",
        "征信",
    )
    return not any(term in clean for term in hard_terms)


def build_intent_context(
    config: dict[str, Any],
    data_capture: dict[str, Any],
    decision: ReplyDecision,
    product_knowledge: dict[str, Any],
    evidence_pack: dict[str, Any],
) -> dict[str, Any]:
    llm_synthesis = config.get("llm_reply_synthesis", {}) or {}
    identity_guard_enabled = llm_synthesis.get("identity_guard_enabled", True) is not False
    return {
        "service_profile": config.get("service_profile", {}) or {},
        "answer_policy": {
            "use_known_facts_only": True,
            "unknown_or_authority_required_action": "handoff",
            "never_invent_price_stock_shipping_or_policy": True,
            "identity_guard_enabled": identity_guard_enabled,
        },
        "data_capture": data_capture,
        "product_knowledge": product_knowledge,
        "evidence_pack": evidence_pack,
        "rule_decision": decision.__dict__,
    }


def conversation_context_from_product_result(product_knowledge: dict[str, Any]) -> dict[str, Any]:
    if not product_knowledge:
        return {}
    preserved_context = {
        key: product_knowledge.get(key)
        for key in (
            "last_customer_need_text",
            "last_customer_need_terms",
            "last_customer_need_updated_at",
            "recent_product_ids",
            "recent_product_updated_at",
        )
        if product_knowledge.get(key) not in (None, "", [], {})
    }
    if product_knowledge.get("last_product_id"):
        return {
            "last_product_id": product_knowledge.get("last_product_id"),
            "last_product_name": product_knowledge.get("last_product_name"),
            "last_quantity": product_knowledge.get("last_quantity"),
            "last_unit_price": product_knowledge.get("last_unit_price") or product_knowledge.get("last_product_price"),
            "last_total": product_knowledge.get("last_total"),
            "last_shipping_city": product_knowledge.get("last_shipping_city"),
            "last_product_price": product_knowledge.get("last_product_price"),
            "last_product_source": product_knowledge.get("last_product_source"),
            **preserved_context,
        }
    return {
        "last_product_id": product_knowledge.get("product_id"),
        "last_product_name": product_knowledge.get("product_name"),
        "last_quantity": product_knowledge.get("quantity"),
        "last_unit_price": product_knowledge.get("unit_price"),
        "last_total": product_knowledge.get("total"),
        "last_shipping_city": product_knowledge.get("shipping_city"),
        **preserved_context,
    }


def product_entity_resolution_settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("product_entity_resolution") if isinstance(config.get("product_entity_resolution"), dict) else {}
    normalized = dict(settings)
    normalized["enabled"] = bool(normalized.get("enabled", False))
    return normalized


def build_realtime_router_evidence_pack(*, combined: str, evidence_pack: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Normalize retrieval output into the compact shape expected by realtime routing."""
    settings = config.get("llm_reply_synthesis", {}) if isinstance(config.get("llm_reply_synthesis"), dict) else {}
    try:
        return compact_knowledge_pack(
            combined,
            evidence_pack,
            max_rag_hits=int(settings.get("max_rag_hits", 5) or 5),
            max_rag_text_chars=int(settings.get("max_rag_text_chars", 900) or 900),
            max_catalog_candidates=int(settings.get("max_catalog_candidates", 8) or 8),
        )
    except Exception:
        return evidence_pack


def build_evidence_context_for_pack(config: dict[str, Any], base_context: dict[str, Any]) -> dict[str, Any]:
    context = dict(base_context or {})
    settings = product_entity_resolution_settings(config)
    if settings:
        context["product_entity_resolution"] = settings
    return context


def summarize_evidence_pack(evidence_pack: dict[str, Any]) -> dict[str, Any]:
    evidence = evidence_pack.get("evidence", {}) or {}
    rag = evidence_pack.get("rag_evidence", {}) or {}
    rag_hits = [
        {
            "chunk_id": item.get("chunk_id"),
            "source_id": item.get("source_id"),
            "score": item.get("score"),
            "category": item.get("category"),
            "source_type": item.get("source_type"),
            "product_id": item.get("product_id"),
            "retrieval_mode": item.get("retrieval_mode"),
            "scoring": item.get("scoring", {}),
            "risk_terms": item.get("risk_terms", []),
            "text": str(item.get("text") or "")[:260],
        }
        for item in rag.get("hits", []) or []
        if isinstance(item, dict)
    ]
    return {
        "scope": evidence_pack.get("scope"),
        "intent_tags": evidence_pack.get("intent_tags", []),
        "selected_item_ids": [item.get("id") for item in evidence_pack.get("selected_items", []) or []],
        "product_ids": [item.get("id") for item in evidence.get("products", []) or [] if item.get("id")],
        "faq_intents": [item.get("intent") for item in evidence.get("faq", []) or [] if item.get("intent")],
        "policy_keys": sorted((evidence.get("policies", {}) or {}).keys()),
        "product_scoped_ids": [item.get("id") for item in evidence.get("product_scoped", []) or [] if item.get("id")],
        "style_example_ids": [item.get("id") for item in evidence.get("style_examples", []) or [] if item.get("id")],
        "rag_chunk_ids": [item.get("chunk_id") for item in rag_hits if item.get("chunk_id")],
        "rag_hits": rag_hits,
        "rag_confidence": rag.get("confidence", 0.0),
        "rag_can_authorize": bool(rag.get("rag_can_authorize", False)),
        "rag_structured_priority": bool(rag.get("structured_priority", True)),
        "safety": evidence_pack.get("safety", {}),
    }


def skipped_intent_assist(
    config: dict[str, Any],
    reason: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = config.get("intent_assist", {}) or {}
    payload: dict[str, Any] = {
        "enabled": bool(settings.get("enabled", False)),
        "skipped": True,
        "reason": reason,
    }
    if settings:
        payload["mode"] = str(settings.get("mode") or "heuristic")
        llm_settings = settings.get("llm_advisory", {}) or {}
        if llm_settings:
            payload["llm_advisory"] = {
                "enabled": bool(llm_settings.get("enabled", False)),
                "skipped": True,
                "reason": reason,
            }
    if extra:
        payload.update(extra)
    return payload


def build_llm_advisory(
    settings: dict[str, Any],
    combined: str,
    context: dict[str, Any],
    heuristic: Any,
) -> dict[str, Any]:
    llm_settings = settings.get("llm_advisory", {}) or {}
    if not llm_settings.get("enabled", False):
        return {"enabled": False}

    provider = resolve_effective_llm_provider(llm_settings.get("provider") or "manual_json")
    prompt_pack = build_llm_prompt_pack(combined, context=context, heuristic=heuristic)
    advisory: dict[str, Any] = {
        "enabled": True,
        "provider": provider,
        "advisory_only": bool(llm_settings.get("advisory_only", True)),
        "schema_version": prompt_pack.get("schema_version"),
        "status": "prompt_pack_ready",
    }
    if llm_settings.get("include_prompt_in_audit", False):
        advisory["prompt_pack"] = prompt_pack

    if provider != "manual_json":
        advisory["status"] = "provider_called"
        advisory["result"] = call_deepseek_advisory(
            combined,
            context=context,
            heuristic=heuristic,
            provider=provider,
            model=str(llm_settings.get("model") or ""),
            base_url=str(llm_settings.get("base_url") or ""),
            timeout=int(llm_settings.get("timeout_seconds", 15)),
        )
        return advisory

    candidate_path_value = str(llm_settings.get("candidate_json_path") or "").strip()
    if not candidate_path_value:
        return advisory

    candidate_path = resolve_path(candidate_path_value)
    advisory["candidate_json_path"] = str(candidate_path)
    if not candidate_path.exists():
        advisory["status"] = "candidate_file_missing"
        return advisory

    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    except Exception as exc:
        advisory["status"] = "candidate_file_error"
        advisory["error"] = repr(exc)
        return advisory

    advisory["status"] = "candidate_validated"
    advisory["validation"] = validate_llm_candidate(candidate, heuristic=heuristic)
    return advisory


def maybe_apply_llm_reply(
    config: dict[str, Any],
    decision: ReplyDecision,
    reply_text: str,
    intent_assist: dict[str, Any],
    product_knowledge: dict[str, Any],
    data_capture: dict[str, Any],
    combined: str = "",
) -> dict[str, Any]:
    settings = (config.get("intent_assist", {}) or {}).get("llm_advisory", {}) or {}
    payload: dict[str, Any] = {
        "enabled": bool(settings.get("enabled", False)),
        "apply_to_reply": bool(settings.get("apply_to_reply", False)),
        "applied": False,
    }
    if not payload["enabled"]:
        payload["reason"] = "llm_disabled"
        return payload
    if not payload["apply_to_reply"]:
        payload["reason"] = "apply_to_reply_disabled"
        return payload
    if brain_first_requires_brain_owned_visible_reply(config):
        payload["reason"] = "brain_first_intent_assist_advisory_only"
        payload["advisory_only"] = True
        payload["legacy_candidate_field"] = "suggested_reply"
        return payload
    if evidence_requires_handoff(intent_assist) or (product_knowledge and product_knowledge.get("needs_handoff")) or (product_knowledge and product_knowledge.get("auto_reply_allowed") is False):
        payload["reason"] = "handoff_required_before_llm_reply"
        return payload

    candidate = llm_candidate_from_intent_assist(intent_assist)
    if not candidate:
        payload["reason"] = "llm_candidate_unavailable"
        payload["llm_status"] = llm_status(intent_assist)
        return payload
    payload["candidate"] = {
        "intent": candidate.get("intent"),
        "confidence": candidate.get("confidence"),
        "recommended_action": candidate.get("recommended_action"),
        "safe_to_auto_send": candidate.get("safe_to_auto_send"),
        "needs_handoff": candidate.get("needs_handoff"),
        "reason": candidate.get("reason"),
    }

    if candidate.get("needs_handoff"):
        if not settings.get("allow_llm_handoff", True):
            payload["reason"] = "llm_handoff_not_allowed"
            return payload
        handoff_text = handoff_acknowledgement_text(config, combined=combined)
        payload.update(
            {
                "applied": True,
                "rule_name": "llm_boundary_handoff",
                "reason": str(candidate.get("reason") or "llm_boundary_handoff"),
                "needs_handoff": True,
                "raw_reply_text": handoff_text,
                "reply_text": format_reply(handoff_text, configured_reply_prefix(config)),
            }
        )
        return payload

    if not candidate.get("safe_to_auto_send"):
        payload["reason"] = "llm_candidate_not_safe_to_auto_send"
        return payload
    try:
        confidence = float(candidate.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    min_confidence = float(settings.get("min_apply_confidence", 0.62) or 0.62)
    if confidence < min_confidence:
        payload["reason"] = "llm_confidence_below_threshold"
        payload["min_confidence"] = min_confidence
        return payload

    suggested_reply = str(candidate.get("suggested_reply") or "").strip()
    if not suggested_reply:
        payload["reason"] = "llm_candidate_empty_reply"
        return payload
    if not llm_reply_allowed_for_decision(settings, candidate, decision, product_knowledge, intent_assist):
        payload["reason"] = "llm_candidate_not_allowed_for_decision"
        return payload

    payload.update(
        {
            "applied": True,
            "rule_name": "llm_boundary_reply",
            "reason": str(candidate.get("reason") or "llm_boundary_auto_reply"),
            "needs_handoff": False,
            "raw_reply_text": suggested_reply,
            "reply_text": format_reply(suggested_reply, configured_reply_prefix(config)),
        }
    )
    return payload


def llm_candidate_from_intent_assist(intent_assist: dict[str, Any]) -> dict[str, Any] | None:
    advisory = intent_assist.get("llm_advisory", {}) or {}
    result = advisory.get("result", {}) or {}
    validation = result.get("validation", {}) or advisory.get("validation", {}) or {}
    if not validation.get("ok"):
        return None
    candidate = validation.get("candidate", {}) or {}
    return candidate if isinstance(candidate, dict) else None


def llm_status(intent_assist: dict[str, Any]) -> dict[str, Any]:
    advisory = intent_assist.get("llm_advisory", {}) or {}
    result = advisory.get("result", {}) or {}
    return {
        "advisory_status": advisory.get("status"),
        "provider": advisory.get("provider"),
        "result_ok": result.get("ok"),
        "result_error": result.get("error"),
        "validation_errors": (result.get("validation", {}) or {}).get("errors", []),
    }


def llm_reply_allowed_for_decision(
    settings: dict[str, Any],
    candidate: dict[str, Any],
    decision: ReplyDecision,
    product_knowledge: dict[str, Any],
    intent_assist: dict[str, Any],
) -> bool:
    if decision.rule_name in {"customer_data_capture", "customer_data_incomplete"}:
        return False
    action = str(candidate.get("recommended_action") or "")
    intent = str(candidate.get("intent") or "")

    if not business_evidence_available(intent_assist, product_knowledge):
        small_talk_actions = {"reply_greeting", "reply_small_talk", "review_or_default_reply"}
        if settings.get("apply_to_small_talk", True) and action in small_talk_actions and intent in {"greeting", "small_talk", "unknown"}:
            return True
        return False

    if not decision.matched or decision.reason == "no_rule_matched":
        return True
    if product_knowledge and product_knowledge.get("matched") and settings.get("apply_to_matched_product", False):
        return True
    policy_actions = {
        "answer_company_info",
        "answer_invoice_policy",
        "answer_payment_policy",
        "answer_logistics_policy",
        "answer_after_sales_policy",
    }
    if action in policy_actions and settings.get("apply_to_policy_reply", False):
        return True
    return False


def business_evidence_available(intent_assist: dict[str, Any], product_knowledge: dict[str, Any] | None) -> bool:
    if product_knowledge and product_knowledge.get("matched"):
        return True
    evidence = intent_assist.get("evidence", {}) or {}
    return bool(
        evidence.get("product_ids")
        or evidence.get("faq_intents")
        or evidence.get("policy_keys")
    )


def data_capture_reply(config: dict[str, Any], data_capture: dict[str, Any], complete: bool) -> str:
    settings = config.get("data_capture", {}) or {}
    if complete:
        contextual = build_data_capture_success_context_reply(data_capture)
        if contextual:
            return contextual
        return str(settings.get("success_reply") or "资料我收到了，后面按这个继续跟进。")
    missing = "、".join(data_capture.get("missing_required_labels", []) or data_capture.get("missing_required_fields", []) or [])
    template = str(
        settings.get("incomplete_reply")
        or "好的，这块我记下了。另外还需要您的{missing_fields}，方便后续跟进，您发我一下就好~"
    )
    return template.format(missing_fields=missing)


def build_data_capture_success_context_reply(data_capture: dict[str, Any]) -> str:
    raw_text = strip_live_marker_suffix(str(data_capture.get("raw_text") or ""))
    fields = data_capture.get("fields") if isinstance(data_capture.get("fields"), dict) else {}
    name = sanitize_lead_display_name(str(fields.get("name") or ""))
    phone = str(fields.get("phone") or "").strip()
    visit_time = extract_visit_time_label(raw_text)
    visit_preference = extract_visit_preference_label(raw_text)
    raw_key = normalize_reply_content_key(raw_text)
    has_trade_in = any(term in raw_key for term in ("旧车", "置换", "开过去", "抵车款"))
    if not (visit_time or visit_preference or has_trade_in):
        return ""
    opener = f"好的{name}，" if name else "好的，"
    first = "电话我记下了" if phone else "资料我记下了"
    context_bits: list[str] = []
    if visit_time and visit_preference:
        context_bits.append(f"{visit_time}{visit_preference}")
    elif visit_time:
        context_bits.append(f"{visit_time}到店")
    if has_trade_in:
        context_bits.append("旧车置换我也一起备注")
    context_text = "，".join(bit for bit in context_bits if bit)
    if context_text:
        return f"{opener}{first}，{context_text}；我先核车源和排期，确认好再回复您。"
    return f"{opener}{first}，我先核车源和排期，确认好再回复您。"


def ensure_data_capture_success_context(reply_text: str, data_capture: dict[str, Any]) -> str:
    if not data_capture.get("complete"):
        return str(reply_text or "").strip()
    raw_text = strip_live_marker_suffix(str(data_capture.get("raw_text") or ""))
    if not raw_text:
        return str(reply_text or "").strip()
    current = str(reply_text or "").strip()
    current_key = normalize_reply_content_key(current)
    missing: list[str] = []
    visit_time = extract_visit_time_label(raw_text)
    visit_preference = extract_visit_preference_label(raw_text)
    current_time_key = normalize_visit_time_key(current)
    if visit_time and normalize_visit_time_key(visit_time) not in current_time_key:
        missing.append(f"{visit_time}到店")
    if visit_preference and not visit_preference_already_covered(visit_preference, current_key):
        missing.append(visit_preference)
    raw_key = normalize_reply_content_key(raw_text)
    if any(term in raw_key for term in ("旧车", "置换", "开过去", "抵车款")) and not any(term in current_key for term in ("旧车", "置换")):
        missing.append("旧车置换")
    if not missing:
        return current
    base = current.rstrip("。")
    suffix = "，".join(unique_list(missing))
    if any(term in current_key for term in ("回复", "回您", "回你", "核实后回", "确认后回")):
        return f"{base}。{suffix}我也一起备注进去。"
    return f"{base}。{suffix}我也一起备注，确认好再回复您。"


def normalize_visit_time_key(text: str) -> str:
    clean = normalize_reply_content_key(text)
    if not clean:
        return ""

    def repl(match: re.Match[str]) -> str:
        hour = chinese_hour_to_int(match.group(1))
        return f"{hour}点" if hour is not None else match.group(0)

    return re.sub(r"([一二三四五六七八九十两]{1,3})点", repl, clean)


def chinese_hour_to_int(value: str) -> int | None:
    text = str(value or "").strip()
    direct = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    if text in direct:
        return direct[text]
    if text.startswith("十") and len(text) == 2:
        tail = direct.get(text[1])
        return 10 + tail if tail is not None else None
    if text.endswith("十") and len(text) == 2:
        head = direct.get(text[0])
        return head * 10 if head is not None else None
    if "十" in text:
        head_text, tail_text = text.split("十", 1)
        head = direct.get(head_text) if head_text else 1
        tail = direct.get(tail_text) if tail_text else 0
        if head is None or tail is None:
            return None
        return head * 10 + tail
    return None


def visit_preference_already_covered(visit_preference: str, current_key: str) -> bool:
    preference_key = normalize_reply_content_key(visit_preference)
    if not preference_key:
        return True
    if preference_key in current_key:
        return True
    # Final visible polish may rewrite "当备选" to "做备选" or similar. For
    # lead-capture acknowledgements, avoid appending the same preference twice
    # when the product names and priority/backup intent are already present.
    product_terms = (
        "奇骏",
        "哈弗",
        "h6",
        "polo",
        "高尔夫",
        "途观",
        "凯美瑞",
        "雅阁",
        "思域",
        "皇冠",
        "宝马",
        "奥迪",
        "特斯拉",
        "model3",
    )
    mentioned_products = [normalize_reply_content_key(term) for term in product_terms if normalize_reply_content_key(term) in preference_key]
    if mentioned_products and not all(term in current_key for term in mentioned_products):
        return False
    if "备选" in preference_key and "备选" not in current_key:
        return False
    priority_markers = ("先看", "优先看", "排前", "更偏向", "先排")
    if any(normalize_reply_content_key(marker) in preference_key for marker in priority_markers):
        return any(normalize_reply_content_key(marker) in current_key for marker in priority_markers)
    return bool(mentioned_products)


def extract_visit_preference_label(raw_text: str) -> str:
    clean = strip_live_marker_suffix(str(raw_text or ""))
    match = re.search(
        r"(?:周[一二三四五六日]|今天|明天|后天)?(?:上午|中午|下午|晚上)?[一二三四五六七八九十两\d]{1,3}点(?:半)?([^。！？!?\n]{0,36})",
        clean,
    )
    if not match:
        return ""
    tail = match.group(1).strip(" ，,；;。")
    tail = re.sub(r"^(到店|过去|来店|去看|过去看|看车|到店看车)[，,；; ]*", "", tail).strip()
    if not tail or tail in {"过去看", "看车", "到店"}:
        return ""
    if any(term in tail for term in ("先看", "优先看", "当备选", "备选")):
        return tail[:36].rstrip("，,；;。")
    return ""


def sanitize_lead_display_name(value: str) -> str:
    clean = re.sub(r"[\s，,。；;：:]+", "", str(value or ""))
    return clean[:8]


def strip_live_marker_suffix(value: str) -> str:
    return re.sub(r"\([^()\n]{8,140}\)\s*$", "", str(value or "").strip()).strip()


def finalize_data_capture_state(target_state: dict[str, Any], data_capture: dict[str, Any]) -> None:
    if not data_capture.get("enabled") or not data_capture.get("is_customer_data"):
        return
    if data_capture.get("complete") and data_capture.get("write_result", {}).get("ok"):
        close_pending_customer_data(target_state, data_capture)
    elif not data_capture.get("complete"):
        upsert_pending_customer_data(target_state, data_capture)


def get_open_pending_customer_data(target_state: dict[str, Any]) -> dict[str, Any] | None:
    pending_items = target_state.get("pending_customer_data", []) or []
    for item in reversed(pending_items):
        if item.get("status") == "waiting_for_fields":
            return item
    return None


def upsert_pending_customer_data(target_state: dict[str, Any], data_capture: dict[str, Any]) -> None:
    pending_items = list(target_state.get("pending_customer_data", []) or [])
    pending = get_open_pending_customer_data(target_state)
    entry = {
        "status": "waiting_for_fields",
        "fields": data_capture.get("fields", {}),
        "missing_required_fields": data_capture.get("missing_required_fields", []),
        "missing_required_labels": data_capture.get("missing_required_labels", []),
        "message_ids": data_capture.get("message_ids", []),
        "raw_text": data_capture.get("raw_text", ""),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if pending:
        entry["created_at"] = pending.get("created_at") or entry["updated_at"]
        for index, item in enumerate(pending_items):
            if item is pending:
                pending_items[index] = entry
                break
        else:
            pending_items.append(entry)
    else:
        entry["created_at"] = entry["updated_at"]
        pending_items.append(entry)
    target_state["pending_customer_data"] = pending_items[-MAX_STORED_IDS:]


def close_pending_customer_data(target_state: dict[str, Any], data_capture: dict[str, Any]) -> None:
    pending_items = list(target_state.get("pending_customer_data", []) or [])
    now = datetime.now().isoformat(timespec="seconds")
    for item in reversed(pending_items):
        if item.get("status") == "waiting_for_fields":
            item["status"] = "completed"
            item["completed_at"] = now
            item["completed_message_ids"] = data_capture.get("message_ids", [])
            item["write_result"] = data_capture.get("write_result")
            break
    target_state["pending_customer_data"] = pending_items[-MAX_STORED_IDS:]


def missing_field_labels(fields: list[str]) -> list[str]:
    labels = {
        "name": "姓名",
        "phone": "电话",
        "address": "地址",
        "product": "产品",
        "quantity": "数量",
        "spec": "规格",
        "budget": "预算",
        "note": "备注",
    }
    return [labels.get(field, field) for field in fields]


def unique_list(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def configured_reply_prefix(config: dict[str, Any]) -> str:
    return str(config.get("reply", {}).get("prefix", BOT_PREFIX + " "))


def bot_reply_prefixes(config: dict[str, Any] | None = None) -> list[str]:
    prefixes = [BOT_PREFIX, BOT_PREFIX + " "]
    if config:
        configured = configured_reply_prefix(config)
        prefixes.extend([configured, configured.rstrip()])
    return unique_list([prefix for prefix in prefixes if prefix])


def is_bot_reply_content(content: str, config: dict[str, Any] | None = None) -> bool:
    stripped = str(content or "").strip()
    if not stripped:
        return False
    if stripped.startswith("[OmniAuto"):
        return True
    return any(stripped.startswith(prefix.strip()) for prefix in bot_reply_prefixes(config))


def evidence_safety(intent_assist: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(intent_assist, dict):
        return {}
    evidence = intent_assist.get("evidence", {}) or {}
    if not isinstance(evidence, dict):
        return {}
    safety = evidence.get("safety", {}) or {}
    return safety if isinstance(safety, dict) else {}


def clear_finance_process_handoff_for_intent_evidence(evidence_pack: dict[str, Any], *, combined: str) -> None:
    if not isinstance(evidence_pack, dict):
        return
    safety = evidence_pack.get("safety", {}) or {}
    if not isinstance(safety, dict) or not safety.get("must_handoff"):
        return
    reasons = {str(item) for item in safety.get("reasons", []) or [] if str(item)}
    if not relax_finance_process_safety(evidence_pack, text=combined, reasons=reasons):
        return
    mark_finance_process_evidence_auto_reply_allowed(evidence_pack)
    safety["must_handoff"] = False
    safety["allowed_auto_reply"] = True
    safety["reasons"] = []
    safety["finance_process_soft_evidence_override"] = True


def evidence_requires_handoff(intent_assist: dict[str, Any] | None) -> bool:
    return bool(evidence_safety(intent_assist).get("must_handoff"))


def customer_data_write_allowed_before_handoff(intent_assist: dict[str, Any] | None) -> bool:
    """Allow lead capture before handoff only when the handoff is not approval/risk driven."""
    safety = evidence_safety(intent_assist)
    reasons = {str(item) for item in safety.get("reasons", []) or [] if str(item)}
    soft_handoff_reasons = {"no_relevant_business_evidence"}
    if safety.get("must_handoff") and not (reasons and reasons <= soft_handoff_reasons):
        return False
    discount_check = safety.get("discount_check", {}) or {}
    if isinstance(discount_check, dict) and discount_check.get("needs_handoff"):
        return False
    blocking_fragments = ("discount", "approval", "below_public_tier", "price_or_policy")
    return not any(fragment in reason for reason in reasons for fragment in blocking_fragments)


def evidence_handoff_reason(intent_assist: dict[str, Any] | None) -> str:
    safety = evidence_safety(intent_assist)
    if not safety.get("must_handoff"):
        return ""
    reasons = [str(item) for item in safety.get("reasons", []) or [] if str(item)]
    if reasons:
        return "evidence_safety:" + ",".join(reasons)
    return "evidence_safety_must_handoff"


def clear_soft_handoff_for_customer_data_complete_ack(
    intent_assist: dict[str, Any] | None,
    *,
    decision: ReplyDecision,
    data_capture: dict[str, Any],
    combined: str,
) -> None:
    """Completed lead capture can acknowledge receipt without inventing facts."""
    if not isinstance(intent_assist, dict):
        return
    if decision.rule_name != "customer_data_capture" or decision.need_handoff:
        return
    if not data_capture.get("is_customer_data") or not data_capture.get("complete"):
        return
    if not customer_data_complete_can_auto_ack(combined):
        return

    safety = evidence_safety(intent_assist)
    reasons = {str(item) for item in safety.get("reasons", []) or [] if str(item)}
    soft_handoff_reasons = {"no_relevant_business_evidence", "auto_reply_disabled"}
    if safety.get("must_handoff") and (not reasons or not reasons <= soft_handoff_reasons):
        return

    safety["must_handoff"] = False
    safety["allowed_auto_reply"] = True
    safety["reasons"] = []
    safety["customer_data_complete_auto_ack_override"] = True
    intent_assist["needs_handoff"] = False
    intent_assist["safe_to_auto_send"] = True
    intent_assist["recommended_action"] = "capture_data_and_confirm"
    reason = str(intent_assist.get("reason") or "").strip()
    if "customer_data_complete_auto_ack" not in reason:
        intent_assist["reason"] = (
            f"{reason}; customer_data_complete_auto_ack" if reason else "customer_data_complete_auto_ack"
        )


def clear_soft_handoff_for_customer_data_incomplete_prompt(
    intent_assist: dict[str, Any] | None,
    *,
    decision: ReplyDecision,
    data_capture: dict[str, Any],
) -> None:
    """Missing required lead fields may be requested without treating it as privacy over-collection."""
    if not isinstance(intent_assist, dict):
        return
    if decision.rule_name != "customer_data_incomplete" or decision.need_handoff:
        return
    if not data_capture.get("is_customer_data") or data_capture.get("complete"):
        return
    missing = [str(item) for item in data_capture.get("missing_required_fields", []) or [] if str(item)]
    if not missing:
        return
    safety = evidence_safety(intent_assist)
    reasons = {str(item) for item in safety.get("reasons", []) or [] if str(item)}
    allowed_reasons = {"matched_faq_requires_handoff", "shared_risk_control", "no_relevant_business_evidence", "auto_reply_disabled"}
    if safety.get("must_handoff") and (not reasons or not reasons <= allowed_reasons):
        return
    safety["must_handoff"] = False
    safety["allowed_auto_reply"] = True
    safety["reasons"] = []
    safety["customer_data_incomplete_prompt_override"] = True
    intent_assist["needs_handoff"] = False
    intent_assist["safe_to_auto_send"] = True
    intent_assist["recommended_action"] = "ask_for_missing_customer_data"
    reason = str(intent_assist.get("reason") or "").strip()
    if "customer_data_incomplete_prompt" not in reason:
        intent_assist["reason"] = (
            f"{reason}; customer_data_incomplete_prompt" if reason else "customer_data_incomplete_prompt"
        )


def clear_no_relevant_handoff_after_safe_synthesis(
    intent_assist: dict[str, Any] | None,
    llm_synthesis: dict[str, Any],
) -> None:
    if not isinstance(intent_assist, dict):
        return
    if not llm_synthesis.get("applied") or llm_synthesis.get("needs_handoff"):
        return
    guard = llm_synthesis.get("guard", {}) or {}
    if guard.get("action") != "send_reply" or guard.get("reason") not in {"guard_passed", "llm_soft_handoff_downgraded"}:
        return
    summary = llm_synthesis.get("evidence_summary", {}) or {}
    advisor_soft_reply = str(llm_synthesis.get("advisor_mode") or "") == "clear_common_sense_recommendation"
    if (
        not advisor_soft_reply
        and int(summary.get("structured_evidence_count") or 0) <= 0
        and int(summary.get("rag_hit_count") or 0) <= 0
    ):
        return
    safety = evidence_safety(intent_assist)
    reasons = {str(item) for item in safety.get("reasons", []) or [] if str(item)}
    intent_tags = {str(item) for item in llm_synthesis.get("intent_tags", []) or [] if str(item)}
    structured_count = int(summary.get("structured_evidence_count") or 0)
    no_relevant_soft_reasons = {"no_relevant_business_evidence", "auto_reply_disabled"}
    product_quote_soft_reasons = {
        "matched_faq_requires_handoff",
        "shared_risk_control",
        "missing_authoritative_evidence",
        "no_relevant_business_evidence",
    }
    safe_product_master_quote_reply = (
        {"product", "quote"} <= intent_tags
        and structured_count > 0
        and bool(reasons)
        and reasons <= product_quote_soft_reasons
    )
    if not reasons or not (reasons <= no_relevant_soft_reasons or safe_product_master_quote_reply):
        return
    safety["must_handoff"] = False
    safety["allowed_auto_reply"] = True
    safety["reasons"] = []
    if safe_product_master_quote_reply:
        safety["llm_synthesis_product_master_quote_override"] = True
    else:
        safety["llm_synthesis_soft_evidence_override"] = True


def clear_no_relevant_handoff_after_safe_realtime_reply(
    intent_assist: dict[str, Any] | None,
    realtime_reply: dict[str, Any],
) -> None:
    if not isinstance(intent_assist, dict) or not realtime_reply.get("applied"):
        return
    safety = evidence_safety(intent_assist)
    reasons = {str(item) for item in safety.get("reasons", []) or [] if str(item)}
    safe_rule_names = {
        "realtime_vehicle_compare_guidance",
        "realtime_vehicle_type_guidance",
        "realtime_maintenance_cost_guidance",
        "realtime_inspection_guidance",
        "realtime_feature_guidance",
        "realtime_comfort_highway_guidance",
        "realtime_finance_guidance",
        "realtime_fee_guidance",
        "realtime_testdrive_materials",
        "realtime_visit_timing",
        "realtime_no_deposit_visit_guidance",
        "realtime_price_negotiation",
        "realtime_warranty_guidance",
        "realtime_trade_in_collect",
        "realtime_value_retention_followup",
        "realtime_new_energy_check",
        "realtime_local_recommendation",
    }
    rule_name = str(realtime_reply.get("rule_name") or "")
    if rule_name == "realtime_catalog_price_fact" and any(str(item) for item in realtime_reply.get("used_product_ids", []) or []):
        safety["must_handoff"] = False
        safety["allowed_auto_reply"] = True
        safety["reasons"] = []
        safety["realtime_reply_safe_rule_override"] = rule_name
        return
    if rule_name in safe_rule_names:
        safety["must_handoff"] = False
        safety["allowed_auto_reply"] = True
        safety["reasons"] = []
        safety["realtime_reply_safe_rule_override"] = rule_name
        return
    if not reasons or not reasons <= {"no_relevant_business_evidence", "auto_reply_disabled"}:
        return
    safety["must_handoff"] = False
    safety["allowed_auto_reply"] = True
    safety["reasons"] = []
    safety["realtime_reply_soft_evidence_override"] = True


def clear_no_relevant_handoff_after_safe_brain_reply(
    intent_assist: dict[str, Any] | None,
    brain_result: dict[str, Any],
) -> None:
    if not isinstance(intent_assist, dict):
        return
    if not brain_result.get("applied") or brain_result.get("needs_handoff"):
        return
    safety = evidence_safety(intent_assist)
    reasons = {str(item) for item in safety.get("reasons", []) or [] if str(item)}
    soft_advisory_handoff_reasons = {
        "matched_faq_requires_handoff",
        "missing_authoritative_evidence",
        "no_relevant_business_evidence",
        "auto_reply_disabled",
    }
    if not reasons or not reasons <= soft_advisory_handoff_reasons:
        return
    rule_name = str(brain_result.get("rule_name") or "")
    if rule_name in {"customer_service_brain_safe_fallback", "customer_service_brain_no_visible_reply"}:
        return
    guard = brain_result.get("guard", {}) or {}
    if guard.get("action") != "send_reply" or guard.get("reason") not in {"guard_passed", "llm_soft_handoff_downgraded"}:
        return
    plan = brain_result.get("brain_plan") if isinstance(brain_result.get("brain_plan"), dict) else {}
    evidence = plan.get("evidence_used") if isinstance(plan.get("evidence_used"), dict) else {}
    common_sense_advisory = bool(evidence.get("common_sense_topics")) and not (
        evidence.get("product_ids") or evidence.get("formal_knowledge_ids") or plan.get("facts_claimed")
    )
    audit = brain_result.get("audit_summary") if isinstance(brain_result.get("audit_summary"), dict) else {}
    authority_sources = brain_result.get("authority_sources") if isinstance(brain_result.get("authority_sources"), dict) else {}
    has_authoritative_evidence = (
        int(audit.get("structured_evidence_count") or 0) > 0
        or bool(evidence.get("product_ids") or evidence.get("formal_knowledge_ids"))
        or bool(plan.get("facts_claimed"))
        or bool(authority_sources.get("product_master") or authority_sources.get("formal_knowledge"))
    )
    if not (common_sense_advisory or has_authoritative_evidence):
        return
    safety["must_handoff"] = False
    safety["allowed_auto_reply"] = True
    safety["reasons"] = []
    safety["customer_service_brain_soft_evidence_override"] = True
    intent_assist["needs_handoff"] = False
    intent_assist["safe_to_auto_send"] = True


def maybe_enrich_messages_with_history(
    *,
    connector: WeChatConnector,
    target: TargetConfig,
    config: dict[str, Any],
    payload: dict[str, Any],
    target_state: dict[str, Any],
) -> dict[str, Any]:
    settings = history_backfill_settings(config)
    if str(settings.get("mode") or "").strip().lower() == "anchor_until_found":
        return maybe_enrich_messages_with_anchor_history(
            connector=connector,
            target=target,
            config=config,
            payload=payload,
            target_state=target_state,
            settings=settings,
        )
    if not settings.get("enabled"):
        enriched = dict(payload)
        enriched["_history_backfill"] = {"enabled": False, "applied": False, "reason": "disabled"}
        return enriched

    messages = payload.get("messages", []) or []
    gap_guard_enabled = bool(settings.get("gap_guard_enabled", True))
    anchor_ids, anchor_content_keys = customer_service_anchor_sets(target_state) if gap_guard_enabled else (set(), set())
    anchor_count = len(anchor_ids) + len(anchor_content_keys)
    initial_anchor_index = find_latest_customer_service_anchor_index(messages, anchor_ids, anchor_content_keys)
    initial_selection = select_batch_details(
        messages,
        target_state=target_state,
        allow_self_for_test=target.allow_self_for_test,
        max_batch_messages=target.max_batch_messages,
        config=config,
    )
    trigger_reasons: list[str] = []
    trigger_count = positive_int(settings.get("trigger_visible_unprocessed_count"), 6)
    saturated_count = positive_int(settings.get("trigger_visible_saturated_count"), 5)
    if initial_selection.eligible_count >= trigger_count:
        trigger_reasons.append("visible_unprocessed_threshold")
    elif initial_selection.eligible_count >= saturated_count and len(messages) <= saturated_count + 1:
        trigger_reasons.append("visible_window_saturated")
    if initial_selection.truncated:
        trigger_reasons.append("batch_truncated")
    if anchor_count and initial_selection.eligible_count > 0 and initial_anchor_index < 0:
        trigger_reasons.append("anchor_missing")
    if not trigger_reasons:
        enriched = dict(payload)
        enriched["_history_backfill"] = {
            "enabled": True,
            "applied": False,
            "reason": "trigger_not_met",
            "eligible_count": initial_selection.eligible_count,
            "trigger_visible_unprocessed_count": trigger_count,
            "trigger_visible_saturated_count": saturated_count,
            "gap_guard_enabled": gap_guard_enabled,
            "anchor_count": anchor_count,
            "anchor_found_initial": initial_anchor_index >= 0,
            "anchor_index_initial": initial_anchor_index,
            "gap_risk": False,
        }
        return enriched

    load_times = bounded_positive_int(
        settings.get("load_times"),
        default=2,
        maximum=positive_int(settings.get("max_load_times"), 5),
    )
    if load_times <= 0:
        enriched = dict(payload)
        gap_risk = history_backfill_gap_risk(
            settings=settings,
            anchor_count=anchor_count,
            anchor_found=initial_anchor_index >= 0,
            selection=initial_selection,
        )
        enriched["_history_backfill"] = {
            "enabled": True,
            "applied": False,
            "reason": "load_times_zero",
            "trigger_reasons": trigger_reasons,
            "gap_guard_enabled": gap_guard_enabled,
            "anchor_count": anchor_count,
            "anchor_found_initial": initial_anchor_index >= 0,
            "anchor_index_initial": initial_anchor_index,
            "gap_risk": gap_risk,
            "gap_reason": "anchor_missing_history_load_disabled" if gap_risk else "",
        }
        return enriched

    try:
        loaded = connector.get_messages(
            target.name,
            exact=target.exact,
            history_load_times=load_times,
            session_key=str(getattr(target, "session_key", "") or ""),
        )
    except Exception as exc:
        enriched = dict(payload)
        gap_risk = history_backfill_gap_risk(
            settings=settings,
            anchor_count=anchor_count,
            anchor_found=initial_anchor_index >= 0,
            selection=initial_selection,
        )
        enriched["_history_backfill"] = {
            "enabled": True,
            "applied": False,
            "reason": "history_load_exception",
            "error": repr(exc),
            "trigger_reasons": trigger_reasons,
            "load_times": load_times,
            "gap_guard_enabled": gap_guard_enabled,
            "anchor_count": anchor_count,
            "anchor_found_initial": initial_anchor_index >= 0,
            "anchor_index_initial": initial_anchor_index,
            "gap_risk": gap_risk,
            "gap_reason": "anchor_missing_history_load_exception" if gap_risk else "",
        }
        return enriched

    if not loaded.get("ok"):
        enriched = dict(payload)
        gap_risk = history_backfill_gap_risk(
            settings=settings,
            anchor_count=anchor_count,
            anchor_found=initial_anchor_index >= 0,
            selection=initial_selection,
        )
        enriched["_history_backfill"] = {
            "enabled": True,
            "applied": False,
            "reason": "history_load_failed",
            "result": loaded,
            "trigger_reasons": trigger_reasons,
            "load_times": load_times,
            "gap_guard_enabled": gap_guard_enabled,
            "anchor_count": anchor_count,
            "anchor_found_initial": initial_anchor_index >= 0,
            "anchor_index_initial": initial_anchor_index,
            "gap_risk": gap_risk,
            "gap_reason": "anchor_missing_history_load_failed" if gap_risk else "",
        }
        return enriched
    sidecar_history_load = loaded.get("history_load") if isinstance(loaded.get("history_load"), dict) else {}
    if sidecar_history_load and sidecar_history_load.get("ok") is False:
        enriched = dict(payload)
        gap_risk = history_backfill_gap_risk(
            settings=settings,
            anchor_count=anchor_count,
            anchor_found=initial_anchor_index >= 0,
            selection=initial_selection,
        )
        enriched["_history_backfill"] = {
            "enabled": True,
            "applied": False,
            "reason": "history_load_failed",
            "trigger_reasons": trigger_reasons,
            "load_times": load_times,
            "sidecar_history_load": sidecar_history_load,
            "gap_guard_enabled": gap_guard_enabled,
            "anchor_count": anchor_count,
            "anchor_found_initial": initial_anchor_index >= 0,
            "anchor_index_initial": initial_anchor_index,
            "gap_risk": gap_risk,
            "gap_reason": "anchor_missing_sidecar_history_load_failed" if gap_risk else "",
        }
        return enriched

    max_messages = positive_int(settings.get("max_messages_after_load"), 80)
    merged_messages = merge_message_windows(
        loaded.get("messages", []) or [],
        messages,
        max_messages=max_messages,
    )
    recovered_anchor_index = find_latest_customer_service_anchor_index(merged_messages, anchor_ids, anchor_content_keys)
    final_selection = select_batch_details(
        merged_messages,
        target_state=target_state,
        allow_self_for_test=target.allow_self_for_test,
        max_batch_messages=target.max_batch_messages,
        config=config,
    )
    gap_risk = history_backfill_gap_risk(
        settings=settings,
        anchor_count=anchor_count,
        anchor_found=recovered_anchor_index >= 0,
        selection=final_selection,
    )
    overflow_batch_on_anchor_missing = bool(settings.get("overflow_batch_on_anchor_missing", True))
    history_continuity = "anchored" if recovered_anchor_index >= 0 else ""
    if (
        gap_risk
        and overflow_batch_on_anchor_missing
        and final_selection.eligible_count > 0
        and anchor_count > 0
        and recovered_anchor_index < 0
    ):
        gap_risk = False
        history_continuity = "overflow_unanchored"
    enriched = dict(loaded)
    enriched["messages"] = merged_messages
    enriched["_history_backfill"] = {
        "enabled": True,
        "applied": True,
        "mechanism": str(sidecar_history_load.get("mechanism") or "rpa.history_load"),
        "trigger_reasons": trigger_reasons,
        "load_times": load_times,
        "initial_message_count": len(messages),
        "loaded_message_count": len(loaded.get("messages", []) or []),
        "final_message_count": len(merged_messages),
        "final_eligible_count": final_selection.eligible_count,
        "final_truncated": final_selection.truncated,
        "sidecar_history_load": sidecar_history_load,
        "gap_guard_enabled": gap_guard_enabled,
        "anchor_count": anchor_count,
        "anchor_found_initial": initial_anchor_index >= 0,
        "anchor_index_initial": initial_anchor_index,
        "anchor_found_after_history_load": recovered_anchor_index >= 0,
        "anchor_index_after_history_load": recovered_anchor_index,
        "history_continuity": history_continuity,
        "overflow_batch": bool(history_continuity == "overflow_unanchored"),
        "gap_risk": gap_risk,
        "gap_reason": "anchor_missing_after_history_load" if gap_risk else "",
    }
    return enriched


def maybe_enrich_messages_with_anchor_history(
    *,
    connector: WeChatConnector,
    target: TargetConfig,
    config: dict[str, Any],
    payload: dict[str, Any],
    target_state: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    if payload.get("scheduler_capture_id"):
        enriched = dict(payload)
        history_meta = payload.get("_history_backfill") if isinstance(payload.get("_history_backfill"), dict) else {}
        enriched["_history_backfill"] = {
            **history_meta,
            "planner_reused_scheduler_capture": True,
            "reason": history_meta.get("reason") or "scheduler_capture_history_reused",
        }
        return enriched

    if not settings.get("enabled"):
        enriched = dict(payload)
        enriched["_history_backfill"] = {
            "enabled": False,
            "mode": "anchor_until_found",
            "applied": False,
            "reason": "disabled",
        }
        return enriched

    messages = payload.get("messages", []) or []
    initial_selection = select_batch_details(
        messages,
        target_state=target_state,
        allow_self_for_test=target.allow_self_for_test,
        max_batch_messages=target.max_batch_messages,
        config=config,
    )
    anchors = customer_service_anchor_payload(target_state)
    anchor_ids = set(anchors.get("anchor_ids", []) or [])
    anchor_content_keys = set(anchors.get("anchor_content_keys", []) or [])
    reply_content_keys = set(anchors.get("reply_content_keys", []) or [])
    anchor_count = len(anchor_ids) + len(anchor_content_keys) + len(reply_content_keys)
    initial_anchor_index = find_latest_customer_service_anchor_index(
        messages,
        anchor_ids,
        anchor_content_keys,
        reply_content_keys=reply_content_keys,
    )

    if initial_anchor_index >= 0:
        messages_after_anchor = messages[initial_anchor_index + 1 :]
        selection_after_anchor = select_batch_details(
            messages_after_anchor,
            target_state=target_state,
            allow_self_for_test=target.allow_self_for_test,
            max_batch_messages=target.max_batch_messages,
            config=config,
        )
        enriched = dict(payload)
        enriched["messages"] = messages_after_anchor
        enriched["_history_backfill"] = {
            "enabled": True,
            "mode": "anchor_until_found",
            "applied": False,
            "reason": "visible_anchor_found_no_scroll",
            "eligible_count": selection_after_anchor.eligible_count,
            "anchor_count": anchor_count,
            "anchor_found_initial": True,
            "anchor_index_initial": initial_anchor_index,
            "messages_after_anchor_count": len(messages_after_anchor),
            "gap_risk": False,
        }
        return enriched

    if initial_selection.eligible_count <= 0:
        enriched = dict(payload)
        enriched["_history_backfill"] = {
            "enabled": True,
            "mode": "anchor_until_found",
            "applied": False,
            "reason": "no_visible_unprocessed_messages",
            "eligible_count": 0,
            "anchor_count": anchor_count,
            "anchor_found_initial": False,
            "anchor_index_initial": -1,
            "gap_risk": False,
        }
        return enriched

    if anchor_count <= 0:
        gap_risk = bool(settings.get("first_window_gap_guard"))
        enriched = dict(payload)
        enriched["_history_backfill"] = {
            "enabled": True,
            "mode": "anchor_until_found",
            "applied": False,
            "reason": "no_anchor_candidates",
            "eligible_count": initial_selection.eligible_count,
            "anchor_count": 0,
            "anchor_found_initial": False,
            "anchor_index_initial": -1,
            "gap_risk": gap_risk,
            "gap_reason": "no_anchor_candidates_for_visible_messages" if gap_risk else "",
        }
        return enriched

    if not bool(settings.get("trigger_when_anchor_missing", True)):
        enriched = dict(payload)
        enriched["_history_backfill"] = {
            "enabled": True,
            "mode": "anchor_until_found",
            "applied": False,
            "reason": "anchor_missing_trigger_disabled",
            "eligible_count": initial_selection.eligible_count,
            "anchor_count": anchor_count,
            "anchor_found_initial": False,
            "anchor_index_initial": -1,
            "gap_risk": False,
        }
        return enriched

    max_scroll_steps = bounded_nonnegative_int(
        settings.get("max_scroll_steps"),
        default=positive_int(settings.get("max_load_times"), 5),
        maximum=16,
    )
    low_volume_fast_path = bool(settings.get("low_volume_fast_path_enabled", True))
    low_volume_eligible_max = bounded_nonnegative_int(settings.get("low_volume_fast_path_max_eligible"), default=2, maximum=8)
    use_low_volume_profile = (
        low_volume_fast_path
        and initial_selection.eligible_count > 0
        and initial_selection.eligible_count <= max(1, low_volume_eligible_max)
        and not initial_selection.truncated
    )
    effective_scroll_steps = max_scroll_steps
    if use_low_volume_profile:
        effective_scroll_steps = min(
            effective_scroll_steps,
            max(1, bounded_nonnegative_int(settings.get("low_volume_fast_path_max_scroll_steps"), default=2, maximum=8)),
        )
    if effective_scroll_steps <= 0:
        enriched = dict(payload)
        enriched["_history_backfill"] = {
            "enabled": True,
            "mode": "anchor_until_found",
            "applied": False,
            "reason": "max_scroll_steps_zero",
            "eligible_count": initial_selection.eligible_count,
            "anchor_count": anchor_count,
            "anchor_found_initial": False,
            "anchor_index_initial": -1,
            "search_profile": "low_volume_fast_path" if use_low_volume_profile else "default",
            "gap_risk": False if bool(settings.get("overflow_batch_on_anchor_missing", True)) else bool(settings.get("block_on_anchor_not_found", settings.get("block_on_gap_risk", True))),
            "gap_reason": "" if bool(settings.get("overflow_batch_on_anchor_missing", True)) else "anchor_missing_history_search_disabled",
            "history_continuity": "overflow_unanchored" if bool(settings.get("overflow_batch_on_anchor_missing", True)) else "",
            "overflow_batch": bool(settings.get("overflow_batch_on_anchor_missing", True)),
        }
        return enriched

    max_duration_seconds = bounded_nonnegative_int(settings.get("max_duration_seconds"), default=12, maximum=60)
    max_snapshots = bounded_nonnegative_int(settings.get("max_snapshots"), default=max_scroll_steps + 2, maximum=24)
    min_delay_ms = bounded_nonnegative_int(settings.get("min_delay_ms"), default=180, maximum=5000)
    max_delay_ms = bounded_nonnegative_int(settings.get("max_delay_ms"), default=650, maximum=10000)
    if use_low_volume_profile:
        max_duration_seconds = min(
            max_duration_seconds,
            max(3, bounded_nonnegative_int(settings.get("low_volume_fast_path_max_duration_seconds"), default=6, maximum=30)),
        )
        max_snapshots = min(
            max_snapshots,
            max(3, bounded_nonnegative_int(settings.get("low_volume_fast_path_max_snapshots"), default=4, maximum=12)),
        )
        min_delay_ms = min(
            min_delay_ms,
            max(50, bounded_nonnegative_int(settings.get("low_volume_fast_path_min_delay_ms"), default=110, maximum=2000)),
        )
        max_delay_ms = min(
            max_delay_ms,
            max(
                min_delay_ms,
                bounded_nonnegative_int(settings.get("low_volume_fast_path_max_delay_ms"), default=320, maximum=3000),
            ),
        )
    if max_snapshots < effective_scroll_steps + 1:
        max_snapshots = effective_scroll_steps + 1

    try:
        loaded = connector.get_messages(
            target.name,
            exact=target.exact,
            history_mode="anchor_until_found",
            anchor_ids=sorted(anchor_ids),
            anchor_content_keys=sorted(anchor_content_keys),
            reply_content_keys=sorted(reply_content_keys),
            max_scroll_steps=effective_scroll_steps,
            max_duration_seconds=max_duration_seconds,
            max_snapshots=max_snapshots,
            min_delay_ms=min_delay_ms,
            max_delay_ms=max_delay_ms,
            restore_to_latest=bool(settings.get("restore_to_latest", True)),
            session_key=str(getattr(target, "session_key", "") or ""),
        )
    except Exception as exc:
        enriched = dict(payload)
        overflow_fallback = bool(settings.get("overflow_batch_on_anchor_missing", True)) and initial_selection.eligible_count > 0
        enriched["_history_backfill"] = {
            "enabled": True,
            "mode": "anchor_until_found",
            "applied": False,
            "reason": "anchor_history_load_exception_overflow_fallback" if overflow_fallback else "anchor_history_load_exception",
            "error": repr(exc),
            "eligible_count": initial_selection.eligible_count,
            "anchor_count": anchor_count,
            "anchor_found_initial": False,
            "anchor_index_initial": -1,
            "search_profile": "low_volume_fast_path" if use_low_volume_profile else "default",
            "search_limits": {
                "max_scroll_steps": effective_scroll_steps,
                "max_duration_seconds": max_duration_seconds,
                "max_snapshots": max_snapshots,
                "min_delay_ms": min_delay_ms,
                "max_delay_ms": max_delay_ms,
            },
            "history_continuity": "overflow_unanchored" if overflow_fallback else "",
            "overflow_batch": overflow_fallback,
            "gap_risk": False if overflow_fallback else True,
            "gap_reason": "" if overflow_fallback else "anchor_missing_history_load_exception",
        }
        return enriched

    sidecar_history_load = loaded.get("history_load") if isinstance(loaded.get("history_load"), dict) else {}
    if not loaded.get("ok") or (sidecar_history_load and sidecar_history_load.get("ok") is False):
        enriched = dict(payload)
        overflow_fallback = bool(settings.get("overflow_batch_on_anchor_missing", True)) and initial_selection.eligible_count > 0
        enriched["_history_backfill"] = {
            "enabled": True,
            "mode": "anchor_until_found",
            "applied": False,
            "reason": "anchor_history_load_failed_overflow_fallback" if overflow_fallback else "anchor_history_load_failed",
            "result": loaded if not loaded.get("ok") else None,
            "sidecar_history_load": sidecar_history_load,
            "eligible_count": initial_selection.eligible_count,
            "anchor_count": anchor_count,
            "anchor_found_initial": False,
            "anchor_index_initial": -1,
            "search_profile": "low_volume_fast_path" if use_low_volume_profile else "default",
            "search_limits": {
                "max_scroll_steps": effective_scroll_steps,
                "max_duration_seconds": max_duration_seconds,
                "max_snapshots": max_snapshots,
                "min_delay_ms": min_delay_ms,
                "max_delay_ms": max_delay_ms,
            },
            "history_continuity": "overflow_unanchored" if overflow_fallback else "",
            "overflow_batch": overflow_fallback,
            "gap_risk": False if overflow_fallback else True,
            "gap_reason": "" if overflow_fallback else "anchor_missing_history_load_failed",
        }
        return enriched

    loaded_messages = loaded.get("messages", []) or []
    loaded_anchor_index = find_latest_customer_service_anchor_index(
        loaded_messages,
        anchor_ids,
        anchor_content_keys,
        reply_content_keys=reply_content_keys,
    )
    loaded_messages_after_anchor = loaded_messages[loaded_anchor_index + 1 :] if loaded_anchor_index >= 0 else loaded_messages
    initial_batch = list(initial_selection.batch) + list(initial_selection.overflow_messages)
    if initial_batch and not message_window_contains_initial_batch(loaded_messages_after_anchor, initial_batch):
        enriched = dict(payload)
        enriched["_history_backfill"] = {
            "enabled": True,
            "mode": "anchor_until_found",
            "applied": bool(sidecar_history_load.get("scroll_steps") or sidecar_history_load.get("anchor_found")),
            "mechanism": str(sidecar_history_load.get("mechanism") or "rpa.anchor_history_load"),
            "trigger_reasons": ["anchor_missing"],
            "reason": "anchor_history_load_dropped_visible_batch_fallback",
            "initial_message_count": len(messages),
            "loaded_message_count": len(loaded_messages),
            "loaded_messages_after_anchor_count": len(loaded_messages_after_anchor),
            "final_message_count": len(messages),
            "messages_after_anchor_count": len(messages),
            "final_eligible_count": initial_selection.eligible_count,
            "final_truncated": initial_selection.truncated,
            "sidecar_history_load": sidecar_history_load,
            "anchor_count": anchor_count,
            "anchor_found_initial": False,
            "anchor_index_initial": -1,
            "anchor_found_after_history_load": loaded_anchor_index >= 0 or bool(sidecar_history_load.get("anchor_found")),
            "anchor_index_after_history_load": loaded_anchor_index,
            "search_profile": "low_volume_fast_path" if use_low_volume_profile else "default",
            "search_limits": {
                "max_scroll_steps": effective_scroll_steps,
                "max_duration_seconds": max_duration_seconds,
                "max_snapshots": max_snapshots,
                "min_delay_ms": min_delay_ms,
                "max_delay_ms": max_delay_ms,
            },
            "history_window_contains_initial_batch": False,
            "fallback_to_initial_window": True,
            "gap_risk": False,
            "gap_reason": "",
        }
        return enriched

    max_messages = positive_int(settings.get("max_messages_after_load"), 80)
    merged_messages = merge_message_windows(
        loaded_messages,
        messages,
        max_messages=max_messages,
    )
    recovered_anchor_index = find_latest_customer_service_anchor_index(
        merged_messages,
        anchor_ids,
        anchor_content_keys,
        reply_content_keys=reply_content_keys,
    )
    messages_after_anchor = merged_messages[recovered_anchor_index + 1 :] if recovered_anchor_index >= 0 else merged_messages
    if initial_batch and not message_window_contains_initial_batch(messages_after_anchor, initial_batch):
        enriched = dict(payload)
        enriched["_history_backfill"] = {
            "enabled": True,
            "mode": "anchor_until_found",
            "applied": bool(sidecar_history_load.get("scroll_steps") or sidecar_history_load.get("anchor_found")),
            "mechanism": str(sidecar_history_load.get("mechanism") or "rpa.anchor_history_load"),
            "trigger_reasons": ["anchor_missing"],
            "reason": "anchor_history_load_dropped_visible_batch_fallback",
            "initial_message_count": len(messages),
            "loaded_message_count": len(loaded_messages),
            "final_message_count": len(merged_messages),
            "messages_after_anchor_count": len(messages_after_anchor),
            "final_eligible_count": initial_selection.eligible_count,
            "final_truncated": initial_selection.truncated,
            "sidecar_history_load": sidecar_history_load,
            "anchor_count": anchor_count,
            "anchor_found_initial": False,
            "anchor_index_initial": -1,
            "anchor_found_after_history_load": recovered_anchor_index >= 0 or bool(sidecar_history_load.get("anchor_found")),
            "anchor_index_after_history_load": recovered_anchor_index,
            "search_profile": "low_volume_fast_path" if use_low_volume_profile else "default",
            "search_limits": {
                "max_scroll_steps": effective_scroll_steps,
                "max_duration_seconds": max_duration_seconds,
                "max_snapshots": max_snapshots,
                "min_delay_ms": min_delay_ms,
                "max_delay_ms": max_delay_ms,
            },
            "history_window_contains_initial_batch": False,
            "fallback_to_initial_window": True,
            "gap_risk": False,
            "gap_reason": "",
        }
        return enriched
    final_selection = select_batch_details(
        messages_after_anchor,
        target_state=target_state,
        allow_self_for_test=target.allow_self_for_test,
        max_batch_messages=target.max_batch_messages,
        config=config,
    )
    anchor_found = recovered_anchor_index >= 0 or bool(sidecar_history_load.get("anchor_found"))
    overflow_batch_on_anchor_missing = bool(settings.get("overflow_batch_on_anchor_missing", True))
    gap_risk = (
        final_selection.eligible_count > 0
        and not anchor_found
        and not overflow_batch_on_anchor_missing
        and bool(settings.get("block_on_anchor_not_found", settings.get("block_on_gap_risk", True)))
    )
    history_continuity = "anchored"
    if not anchor_found and overflow_batch_on_anchor_missing and final_selection.eligible_count > 0:
        history_continuity = "overflow_unanchored"
    enriched = dict(loaded)
    enriched["messages"] = messages_after_anchor
    enriched["_history_backfill"] = {
        "enabled": True,
        "mode": "anchor_until_found",
        "applied": bool(sidecar_history_load.get("scroll_steps") or sidecar_history_load.get("anchor_found")),
        "mechanism": str(sidecar_history_load.get("mechanism") or "rpa.anchor_history_load"),
        "trigger_reasons": ["anchor_missing"],
        "initial_message_count": len(messages),
        "loaded_message_count": len(loaded_messages),
        "loaded_messages_after_anchor_count": len(loaded_messages_after_anchor),
        "final_message_count": len(merged_messages),
        "messages_after_anchor_count": len(messages_after_anchor),
        "final_eligible_count": final_selection.eligible_count,
        "final_truncated": final_selection.truncated,
        "sidecar_history_load": sidecar_history_load,
        "anchor_count": anchor_count,
        "anchor_found_initial": False,
        "anchor_index_initial": -1,
        "anchor_found_after_history_load": anchor_found,
        "anchor_index_after_history_load": recovered_anchor_index,
        "search_profile": "low_volume_fast_path" if use_low_volume_profile else "default",
        "search_limits": {
            "max_scroll_steps": effective_scroll_steps,
            "max_duration_seconds": max_duration_seconds,
            "max_snapshots": max_snapshots,
            "min_delay_ms": min_delay_ms,
            "max_delay_ms": max_delay_ms,
        },
        "history_window_contains_initial_batch": True,
        "history_continuity": history_continuity,
        "overflow_batch": bool(history_continuity == "overflow_unanchored"),
        "gap_risk": gap_risk,
        "gap_reason": "anchor_missing_after_bounded_history_search" if gap_risk else "",
    }
    return enriched


def message_window_contains_initial_batch(messages: list[dict[str, Any]], initial_batch: list[dict[str, Any]]) -> bool:
    original_ids = {reply_input_message_identity(item) for item in initial_batch if isinstance(item, dict) and reply_input_message_identity(item)}
    original_content_keys = {
        key
        for item in initial_batch
        if isinstance(item, dict)
        for key in message_original_match_keys(item)
        if key
    }
    if not original_ids and not original_content_keys:
        return True
    return any(
        isinstance(message, dict) and message_matches_original_batch(message, original_ids, original_content_keys)
        for message in messages or []
    )


def history_backfill_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if isinstance(config, dict) else {}
    settings = dict(cfg.get("history_backfill", {}) or {})
    settings.setdefault("enabled", True)
    settings.setdefault("mode", "fixed_load_times")
    settings.setdefault("load_times", 2)
    settings.setdefault("max_load_times", 5)
    settings.setdefault("max_scroll_steps", settings.get("max_load_times", 5))
    settings.setdefault("max_duration_seconds", 12)
    settings.setdefault("max_snapshots", 8)
    settings.setdefault("min_delay_ms", 180)
    settings.setdefault("max_delay_ms", 650)
    settings.setdefault("low_volume_fast_path_enabled", True)
    settings.setdefault("low_volume_fast_path_max_eligible", 2)
    settings.setdefault("low_volume_fast_path_max_scroll_steps", 2)
    settings.setdefault("low_volume_fast_path_max_duration_seconds", 6)
    settings.setdefault("low_volume_fast_path_max_snapshots", 4)
    settings.setdefault("low_volume_fast_path_min_delay_ms", 110)
    settings.setdefault("low_volume_fast_path_max_delay_ms", 320)
    settings.setdefault("restore_to_latest", True)
    settings.setdefault("trigger_when_anchor_missing", True)
    settings.setdefault("block_on_anchor_not_found", settings.get("block_on_gap_risk", True))
    settings.setdefault("overflow_batch_on_anchor_missing", True)
    settings.setdefault("trigger_visible_unprocessed_count", 6)
    settings.setdefault("trigger_visible_saturated_count", 5)
    settings.setdefault("max_messages_after_load", 80)
    settings.setdefault("freshness_load_times", settings.get("load_times", 2))
    settings.setdefault("freshness_anchor_scroll_enabled", False)
    settings.setdefault("freshness_anchor_max_scroll_steps", 0)
    settings.setdefault("gap_guard_enabled", True)
    settings.setdefault("block_on_gap_risk", True)
    settings.setdefault("first_window_gap_guard", False)
    return settings


def merge_message_windows(*windows: list[dict[str, Any]], max_messages: int = 80) -> list[dict[str, Any]]:
    window_content_keys = [
        {
            content_key
            for item in window or []
            if isinstance(item, dict)
            for content_key in [message_content_dedupe_key(item)]
            if content_key
        }
        for window in windows
    ]
    future_content_keys: list[set[str]] = []
    future: set[str] = set()
    for keys in reversed(window_content_keys):
        future_content_keys.append(set(future))
        future.update(keys)
    future_content_keys.reverse()

    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for index, window in enumerate(windows):
        for item in window or []:
            if not isinstance(item, dict):
                continue
            key = message_dedupe_key(item)
            if not key or key in seen:
                continue
            content_key = message_content_dedupe_key(item)
            if content_key and content_key in future_content_keys[index]:
                continue
            seen.add(key)
            merged.append(item)
    if max_messages > 0 and len(merged) > max_messages:
        return merged[-max_messages:]
    return merged


def message_dedupe_key(message: dict[str, Any]) -> str:
    message_id = str(message.get("id") or "").strip()
    if message_id:
        return f"id:{message_id}"
    return "fallback:" + hashlib.sha256(
        json.dumps(
            {
                "sender": message.get("sender"),
                "type": message.get("type"),
                "time": message.get("time"),
                "content": message.get("content"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]


def stable_workflow_id(prefix: str, *parts: Any) -> str:
    seed = json.dumps([str(item) for item in parts], ensure_ascii=False, sort_keys=True)
    return f"{prefix}_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def reply_input_repeatable_occurrence_identity(message: dict[str, Any]) -> str:
    """Return an occurrence-aware id for short repeatable customer probes.

    The scheduler state layer already uses this idea. Keep the workflow layer
    aligned so a later "在不/人呢/好的" turn is not swallowed by raw OCR ids or
    content-key de-dupe before it reaches Brain.
    """

    if not message_has_repeatable_probe_content(message):
        return ""
    sender = str(message.get("sender") or "").strip()
    msg_type = str(message.get("type") or "").strip()
    content = normalize_greeting_probe_text(str(message.get("content") or ""))
    base_id = str(message.get("id") or message.get("message_id") or "").strip()
    base_id_lower = base_id.lower()
    ocr_like_id = base_id_lower.startswith(("win32_ocr:", "ocr:", "screen_ocr:", "uia_ocr:"))
    if base_id and not ocr_like_id:
        return base_id
    occurrence = ""
    for key in (
        "pending_signal_id",
        "pending_since",
        "last_detected_at",
        "message_time",
        "screen_time_text",
        "time",
        "captured_at",
        "created_at",
    ):
        occurrence = str(message.get(key) or "").strip()
        if occurrence:
            break
    if occurrence:
        return stable_workflow_id("repeatable_msg", sender, msg_type, content, base_id, occurrence)
    if base_id:
        return stable_workflow_id("repeatable_msg", sender, msg_type, content, base_id)
    return ""


def reply_input_message_identity(message: dict[str, Any]) -> str:
    canonical_id = canonical_input_message_id(message)
    if canonical_id:
        return canonical_id
    repeatable_id = reply_input_repeatable_occurrence_identity(message)
    if repeatable_id:
        return repeatable_id
    return str(message.get("id") or message.get("message_id") or "").strip()


def message_has_occurrence_identity_signal(message: dict[str, Any]) -> bool:
    return bool(occurrence_marker_for_message(message))


def message_content_dedupe_key(message: dict[str, Any]) -> str:
    if message_has_repeatable_probe_content(message):
        return ""
    content = normalize_ocr_content_for_dedupe(str(message.get("content") or ""))
    if not content:
        return ""
    return "\x1f".join(
        [
            str(message.get("sender") or "").strip(),
            str(message.get("type") or "").strip(),
            content,
        ]
    )


def normalize_ocr_content_for_dedupe(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or "")).lower()


def positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def bounded_positive_int(value: Any, *, default: int, maximum: int) -> int:
    parsed = positive_int(value, default)
    return max(0, min(maximum, parsed))


def bounded_nonnegative_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(0, min(maximum, parsed))


BOOTSTRAP_PENDING_VISIBLE_MESSAGE = "当前不在可见会话列表里，待收到新消息时会自动识别"
BOOTSTRAP_SELF_SENDERS = {"self", "assistant", "agent", "me", "outbound"}


def truthy_setting(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def bootstrap_visible_only_target_confirmation_enabled(config: dict[str, Any] | None) -> bool:
    cfg = config if isinstance(config, dict) else {}
    bootstrap = cfg.get("bootstrap") if isinstance(cfg.get("bootstrap"), dict) else {}
    guard = cfg.get("live_safety_guard") if isinstance(cfg.get("live_safety_guard"), dict) else {}
    value = bootstrap.get("visible_only_target_confirmation")
    if value is None:
        value = guard.get("startup_bootstrap_visible_only")
    return truthy_setting(value, default=True)


def bootstrap_mark_customer_messages_processed(config: dict[str, Any] | None, *, visible_only: bool) -> bool:
    cfg = config if isinstance(config, dict) else {}
    bootstrap = cfg.get("bootstrap") if isinstance(cfg.get("bootstrap"), dict) else {}
    value = bootstrap.get("mark_customer_messages_processed")
    if value is not None:
        return truthy_setting(value, default=False)
    # Visible-only startup bootstrap is a safety/readiness check, not a history
    # closeout pass.  Marking customer bubbles here can swallow unread messages
    # after a restart; keep the legacy mark-all behavior only for explicit
    # history bootstrap/backfill modes.
    return not visible_only


def bootstrap_message_can_be_marked_processed(
    message: dict[str, Any],
    *,
    config: dict[str, Any] | None,
    mark_customer_messages: bool,
) -> bool:
    sender = str(message.get("sender") or "").strip().lower()
    if sender in BOOTSTRAP_SELF_SENDERS:
        return True
    if mark_customer_messages:
        return True
    return False


def bootstrap_payload_can_wait_for_visible_target(payload: dict[str, Any] | None) -> bool:
    data = payload if isinstance(payload, dict) else {}
    if data.get("target_pending_visible") is True:
        return True
    state = str(data.get("state") or "")
    if state not in {"target_not_confirmed_for_messages", "target_not_visible_for_messages"}:
        return False
    if data.get("opened") not in {False, None}:
        return False
    guard = data.get("guard") if isinstance(data.get("guard"), dict) else {}
    guard_state = str(guard.get("state") or "")
    guard_reason = str(guard.get("reason") or "")
    hard_states = {"blank_render_detected", "login_window_detected", "auxiliary_shell_window_detected"}
    if state in hard_states or guard_state in hard_states or guard_reason in {"blank_render", "login_or_qr", "auxiliary_shell_window"}:
        return False
    return True


def mark_bootstrap_pending_visible(target: TargetConfig, target_state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    marker = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "reason": str(payload.get("reason") or payload.get("state") or "target_not_visible_waiting_for_unread"),
        "message": BOOTSTRAP_PENDING_VISIBLE_MESSAGE,
    }
    target_state["bootstrap_pending_visible"] = marker
    return base_event(
        target,
        "pending_visible",
        {
            "reason": marker["reason"],
            "message": marker["message"],
            "messages": payload,
        },
    )


def bootstrap_target(
    connector: WeChatConnector,
    target: TargetConfig,
    state: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    target_state = state.setdefault("targets", {}).setdefault(
        target.name,
        {
            "processed_message_ids": [],
            "processed_content_keys": [],
            "handoff_message_ids": [],
            "sent_replies": [],
            "reply_timestamps": [],
        },
    )
    load_times = bootstrap_history_load_times(config)
    visible_only = bootstrap_visible_only_target_confirmation_enabled(config)
    if visible_only:
        load_times = 0
    clean_session_key = str(getattr(target, "session_key", "") or "")
    try:
        latest_payload = connector.get_messages(
            target.name,
            exact=target.exact,
            history_load_times=0,
            visible_only_target=visible_only,
            session_key=clean_session_key,
        )
    except TypeError as exc:
        if clean_session_key:
            return base_event(target, "error", {"messages": {"ok": False, "reason": "connector_session_key_not_supported", "error": repr(exc)}})
        latest_payload = connector.get_messages(target.name, exact=target.exact)
    if not latest_payload.get("ok"):
        if visible_only and bootstrap_payload_can_wait_for_visible_target(latest_payload):
            return mark_bootstrap_pending_visible(target, target_state, latest_payload)
        return base_event(target, "error", {"messages": latest_payload})
    payload = latest_payload
    if load_times > 0:
        try:
            history_payload = connector.get_messages(
                target.name,
                exact=target.exact,
                history_load_times=load_times,
                session_key=clean_session_key,
            )
        except TypeError as exc:
            if clean_session_key:
                return base_event(target, "error", {"messages": {"ok": False, "reason": "connector_session_key_not_supported", "error": repr(exc)}})
            history_payload = connector.get_messages(target.name, exact=target.exact)
        if not history_payload.get("ok"):
            return base_event(target, "error", {"messages": history_payload})
        payload = history_payload
        payload["_bootstrap_latest_snapshot"] = latest_payload

    processed = list(target_state.get("processed_message_ids", []))
    processed_content_keys = list(target_state.get("processed_content_keys", []))
    added = []
    deferred_customer_message_ids: list[str] = []
    candidates: list[dict[str, Any]] = []
    for source_payload in (latest_payload, payload):
        for message in source_payload.get("messages", []) or []:
            if isinstance(message, dict):
                candidates.append(message)
    mark_customer_messages = bootstrap_mark_customer_messages_processed(config, visible_only=visible_only)
    for message in candidates:
        message_id = reply_input_message_identity(message)
        content = str(message.get("content") or "").strip()
        if not message_id or not content or message.get("type") != "text":
            continue
        if is_bot_reply_content(content, config):
            continue
        if not bootstrap_message_can_be_marked_processed(
            message,
            config=config,
            mark_customer_messages=mark_customer_messages,
        ):
            if message_id not in deferred_customer_message_ids:
                deferred_customer_message_ids.append(message_id)
            continue
        content_keys = message_processed_content_keys(message)
        if message_id in processed:
            continue
        if any(key in processed_content_keys for key in content_keys):
            continue
        processed.append(message_id)
        append_unique_limited(processed_content_keys, content_keys)
        added.append(message_id)
    target_state["processed_message_ids"] = processed[-MAX_STORED_IDS:]
    target_state["processed_content_keys"] = processed_content_keys[-MAX_STORED_IDS:]
    target_state.pop("bootstrap_pending_visible", None)
    target_state.setdefault("bootstrap_events", []).append(
        {
            "message_ids": added,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    return base_event(
        target,
        "bootstrapped",
        {
            "marked_message_ids": added,
            "marked_count": len(added),
            "history_load_times": load_times,
            "history_load": payload.get("history_load") if isinstance(payload.get("history_load"), dict) else {},
            "latest_visible_count": len(latest_payload.get("messages", []) or []),
            "deferred_customer_message_ids": deferred_customer_message_ids,
            "deferred_customer_count": len(deferred_customer_message_ids),
            "mark_customer_messages_processed": mark_customer_messages,
        },
    )


def bootstrap_history_load_times(config: dict[str, Any] | None = None) -> int:
    cfg = config if isinstance(config, dict) else {}
    settings = dict(cfg.get("bootstrap", {}) or {})
    history_settings = dict(cfg.get("history_backfill", {}) or {})
    raw_value = settings.get("history_load_times")
    if raw_value is None:
        raw_value = history_settings.get("bootstrap_load_times", history_settings.get("load_times", 2))
    max_value = positive_int(settings.get("max_history_load_times", history_settings.get("max_load_times", 5)), 5)
    return bounded_nonnegative_int(raw_value, default=2, maximum=max_value)


def select_batch(
    messages: list[dict[str, Any]],
    target_state: dict[str, Any],
    allow_self_for_test: bool,
    max_batch_messages: int,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return select_batch_details(
        messages,
        target_state=target_state,
        allow_self_for_test=allow_self_for_test,
        max_batch_messages=max_batch_messages,
        config=config,
    ).batch


def select_batch_details(
    messages: list[dict[str, Any]],
    target_state: dict[str, Any],
    allow_self_for_test: bool,
    max_batch_messages: int,
    config: dict[str, Any] | None = None,
) -> BatchSelection:
    processed = set(target_state.get("processed_message_ids", []))
    processed_content_keys = set(target_state.get("processed_content_keys", []))
    handoff = set(target_state.get("handoff_message_ids", []))
    sent_reply_content_keys = recent_sent_reply_content_keys(target_state)
    selected: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    try:
        limit = max(1, int(max_batch_messages or DEFAULT_MAX_BATCH_MESSAGES))
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_BATCH_MESSAGES
    for message in reversed(messages):
        if not message_is_reply_candidate(
            message,
            processed=processed,
            processed_content_keys=processed_content_keys,
            handoff=handoff,
            allow_self_for_test=allow_self_for_test,
            config=config,
            sent_reply_content_keys=sent_reply_content_keys,
        ):
            if selected or overflow:
                break
            continue
        if len(selected) < limit:
            selected.append(message)
        else:
            overflow.append(message)
    batch = list(reversed(selected))
    overflow_messages = list(reversed(overflow))
    return BatchSelection(
        batch=batch,
        overflow_messages=overflow_messages,
        eligible_count=len(batch) + len(overflow_messages),
        max_batch_messages=limit,
    )


def select_scheduler_authoritative_batch_details(
    payload: dict[str, Any],
    *,
    target_state: dict[str, Any],
    allow_self_for_test: bool,
    max_batch_messages: int,
    config: dict[str, Any] | None = None,
) -> BatchSelection:
    """Use the scheduler-confirmed capture batch when legacy de-dupe is too strict.

    Scheduler captures are created after unread/session monitoring has already
    decided that a conversation needs a reply.  They are a stronger conversation
    state signal than a raw OCR content-key match, but they still do not author
    customer-visible text; the selected batch must continue through Brain.
    """

    try:
        limit = max(1, int(max_batch_messages or DEFAULT_MAX_BATCH_MESSAGES))
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_BATCH_MESSAGES
    if not isinstance(payload, dict) or payload.get("_scheduler_capture_is_authoritative") is not True:
        return BatchSelection(batch=[], overflow_messages=[], eligible_count=0, max_batch_messages=limit)
    raw_id_list = [
        str(item or "").strip()
        for item in (payload.get("_scheduler_authoritative_batch_ids") or [])
        if str(item or "").strip()
    ]
    raw_ids = set(raw_id_list)
    raw_batch = [item for item in (payload.get("_scheduler_authoritative_batch") or []) if isinstance(item, dict)]
    normalized_messages = [item for item in (payload.get("messages") or []) if isinstance(item, dict)]
    by_id = {
        reply_input_message_identity(item): item
        for item in normalized_messages
        if reply_input_message_identity(item)
    }
    if raw_id_list:
        authoritative_messages = [by_id.get(message_id) for message_id in raw_id_list if isinstance(by_id.get(message_id), dict)]
        if len(authoritative_messages) < len(raw_ids):
            for item in raw_batch:
                message_id = reply_input_message_identity(item)
                if message_id and message_id in raw_ids and not any(reply_input_message_identity(existing) == message_id for existing in authoritative_messages if isinstance(existing, dict)):
                    authoritative_messages.append(item)
    else:
        authoritative_messages = raw_batch
    processed = set(target_state.get("processed_message_ids", []))
    handoff = set(target_state.get("handoff_message_ids", []))
    sent_reply_content_keys = recent_sent_reply_content_keys(target_state)
    valid: list[dict[str, Any]] = []
    for item in authoritative_messages:
        if not isinstance(item, dict):
            continue
        if not scheduler_authoritative_message_is_reply_candidate(
            item,
            processed=processed,
            handoff=handoff,
            allow_self_for_test=allow_self_for_test,
            config=config,
            sent_reply_content_keys=sent_reply_content_keys,
        ):
            continue
        valid.append(item)
    overflow = valid[:-limit] if len(valid) > limit else []
    batch = valid[-limit:]
    return BatchSelection(
        batch=batch,
        overflow_messages=overflow,
        eligible_count=len(valid),
        max_batch_messages=limit,
    )


def scheduler_authoritative_message_is_reply_candidate(
    message: dict[str, Any],
    *,
    processed: set[str],
    handoff: set[str],
    allow_self_for_test: bool,
    config: dict[str, Any] | None = None,
    sent_reply_content_keys: set[str] | None = None,
) -> bool:
    message_id = reply_input_message_identity(message)
    content = str(message.get("content") or "").strip()
    sender = str(message.get("sender") or "")
    if not message_id or message_id in processed or message_id in handoff:
        return False
    if message.get("type") != "text" or not content:
        return False
    if message_is_visual_or_media_ocr_noise(message):
        return False
    if is_bot_reply_content(content, config):
        return False
    if sender == "self" and not allow_self_for_test:
        return False
    if sender == "self" and allow_self_for_test and sent_reply_content_keys:
        if normalize_reply_content_key(content) in sent_reply_content_keys:
            return False
    return True


def message_is_reply_candidate(
    message: dict[str, Any],
    *,
    processed: set[str],
    processed_content_keys: set[str] | None = None,
    handoff: set[str],
    allow_self_for_test: bool,
    config: dict[str, Any] | None = None,
    sent_reply_content_keys: set[str] | None = None,
) -> bool:
    message_id = reply_input_message_identity(message)
    content = str(message.get("content") or "").strip()
    sender = str(message.get("sender") or "")
    if not message_id or message_id in processed or message_id in handoff:
        return False
    repeatable_probe = message_has_repeatable_probe_content(message)
    stable_key = message_stable_content_key(message)
    content_key = message_content_dedupe_key(message)
    if (
        not repeatable_probe
        and not message_has_occurrence_identity_signal(message)
        and processed_content_keys
        and ((stable_key and stable_key in processed_content_keys) or (content_key and content_key in processed_content_keys))
    ):
        return False
    if message.get("type") != "text" or not content:
        return False
    if message_is_visual_or_media_ocr_noise(message):
        return False
    if is_bot_reply_content(content, config):
        return False
    if sender == "self" and not allow_self_for_test:
        return False
    if sender == "self" and allow_self_for_test and sent_reply_content_keys:
        if normalize_reply_content_key(content) in sent_reply_content_keys:
            return False
    return True


def batch_selection_payload(selection: BatchSelection) -> dict[str, Any]:
    overflow = selection.overflow_messages
    return {
        "message_ids": [reply_input_message_identity(item) for item in selection.batch if reply_input_message_identity(item)],
        "overflow_message_ids": [reply_input_message_identity(item) for item in overflow if reply_input_message_identity(item)],
        "overflow_count": len(overflow),
        "eligible_count": selection.eligible_count,
        "max_batch_messages": selection.max_batch_messages,
        "truncated": selection.truncated,
    }


def normalize_capture_payload_for_semantic_processing(payload: dict[str, Any], *, target: TargetConfig, config: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    conversation_type = str(((config or {}).get("raw_messages", {}) or {}).get("conversation_type") or infer_raw_conversation_type(target.name))
    known_speakers = known_speaker_names_for_target(target, config)
    normalized_payload = dict(payload)
    normalized_messages, meta = normalize_messages_for_semantic_processing(
        [item for item in payload.get("messages", []) or [] if isinstance(item, dict)],
        conversation_type=conversation_type,
        target_name=target.name,
        known_speakers=known_speakers,
        allow_unlisted_name_like_prefix=True,
    )
    normalized_payload["messages"] = normalized_messages
    if meta["changed_count"]:
        normalized_payload["_content_normalization"] = meta
    latest_snapshot = payload.get("_bootstrap_latest_snapshot")
    if isinstance(latest_snapshot, dict):
        latest_messages, latest_meta = normalize_messages_for_semantic_processing(
            [item for item in latest_snapshot.get("messages", []) or [] if isinstance(item, dict)],
            conversation_type=conversation_type,
            target_name=target.name,
            known_speakers=known_speakers,
            allow_unlisted_name_like_prefix=True,
        )
        normalized_payload["_bootstrap_latest_snapshot"] = {
            **latest_snapshot,
            "messages": latest_messages,
            **({"_content_normalization": latest_meta} if latest_meta["changed_count"] else {}),
        }
    return normalized_payload


def normalize_messages_for_semantic_processing(
    messages: list[dict[str, Any]],
    *,
    conversation_type: str,
    target_name: str,
    known_speakers: list[str] | tuple[str, ...] | set[str] | None = None,
    allow_unlisted_name_like_prefix: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        next_item = normalize_wechat_message_record(
            item,
            conversation_type=conversation_type,
            target_name=target_name,
            known_speakers=known_speakers,
            allow_unlisted_name_like_prefix=allow_unlisted_name_like_prefix,
        )
        envelope = build_message_envelope(
            next_item,
            source_adapter=str(next_item.get("source_adapter") or ""),
            conversation={
                "target_name": target_name,
                "conversation_type": conversation_type,
            },
        )
        next_item = apply_message_envelope_to_record(next_item, envelope)
        next_item = apply_canonical_identity_fields(
            next_item,
            target_name=target_name,
            conversation_type=conversation_type,
        )
        if next_item.get("ocr_speaker_prefix") or next_item.get("quoted_fragments") or next_item.get("quality_flags"):
            changed.append(
                {
                    "id": str(next_item.get("id") or next_item.get("message_id") or ""),
                    "speaker_name": str(next_item.get("speaker_name") or ""),
                    "original_preview": str(next_item.get("original_content") or "")[:120],
                    "content_preview": str(next_item.get("content") or "")[:120],
                    "quality_flags": list(next_item.get("quality_flags") or []),
                    "quoted_fragment_count": len(next_item.get("quoted_fragments") or []),
                }
            )
        normalized.append(next_item)
    return normalized, {
        "changed_count": len(changed),
        "changed_messages": changed[:20],
        "conversation_type": conversation_type,
        "target_name": target_name,
    }


def normalize_batch_content_for_reply_routing(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in batch:
        if not isinstance(item, dict):
            continue
        next_item = dict(item)
        content = str(next_item.get("content") or "")
        cleaned = strip_leading_group_speaker_line(content)
        if cleaned != content:
            next_item["content"] = cleaned
            next_item["_reply_routing_content_normalized"] = True
            next_item["_reply_routing_original_content"] = content
        normalized.append(next_item)
    return normalized


def batch_routing_normalization_payload(original: list[dict[str, Any]], normalized: list[dict[str, Any]]) -> dict[str, Any]:
    changed: list[dict[str, Any]] = []
    for before, after in zip(original, normalized):
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        before_text = str(before.get("content") or "")
        after_text = str(after.get("content") or "")
        if before_text == after_text:
            continue
        changed.append(
            {
                "message_id": str(before.get("id") or ""),
                "reason": "leading_group_speaker_prefix_removed",
                "before_preview": before_text[:80],
                "after_preview": after_text[:80],
            }
        )
    return {
        "applied": bool(changed),
        "changed_count": len(changed),
        "changed": changed[:8],
    }


def plan_message_batch_semantics(batch: list[dict[str, Any]], config: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = semantic_batch_settings(config)
    texts = [str(item.get("content") or "").strip() for item in batch if str(item.get("content") or "").strip()]
    if not texts:
        return {
            "enabled": bool(settings.get("enabled")),
            "kind": "empty",
            "reply_strategy": "skip",
            "risk_level": "normal",
            "combined_text": "",
            "segments": [],
        }
    if not settings.get("enabled") or len(texts) == 1:
        text = texts[0] if len(texts) == 1 else "\n".join(texts)
        return {
            "enabled": bool(settings.get("enabled")),
            "kind": "single_event",
            "reply_strategy": "answer_as_one_need",
            "risk_level": "normal",
            "combined_text": text,
            "segments": build_semantic_segments(texts),
        }

    max_messages = positive_int(settings.get("max_messages"), 12)
    considered = texts[-max_messages:]
    segments = build_semantic_segments(considered)
    categories = {category for segment in segments for category in segment.get("categories", [])}
    risk_categories = categories & {"document_boundary", "identity_probe", "off_topic", "after_sales_dispute", "price_approval"}

    if looks_like_spam_or_noise(considered, threshold=float(settings.get("spam_repeat_threshold", 0.72) or 0.72)):
        kind = "spam_or_noise"
        strategy = "short_stabilize_and_do_not_answer_every_line"
        risk_level = "attention"
    elif risk_categories and len(categories - risk_categories) > 0:
        kind = "multi_question_mixed_risk"
        strategy = "answer_safe_parts_and_defer_risk_parts"
        risk_level = "boundary"
    elif is_single_need_fragment_batch(segments):
        kind = "single_event"
        strategy = "answer_as_one_need"
        risk_level = "normal"
    elif len(categories) > 1:
        kind = "multi_question_same_scene"
        strategy = "answer_in_natural_points"
        risk_level = "normal" if not risk_categories else "boundary"
    else:
        kind = "single_event"
        strategy = "answer_as_one_need"
        risk_level = "normal"

    return {
        "enabled": True,
        "kind": kind,
        "reply_strategy": strategy,
        "risk_level": risk_level,
        "combined_text": build_semantic_combined_text(kind, considered),
        "segments": segments,
        "message_count": len(texts),
        "considered_count": len(considered),
    }


def semantic_batch_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config if isinstance(config, dict) else {}
    settings = dict(cfg.get("semantic_batch_planner", {}) or {})
    settings.setdefault("enabled", True)
    settings.setdefault("max_messages", 12)
    settings.setdefault("spam_repeat_threshold", 0.72)
    return settings


def build_semantic_segments(texts: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "index": index + 1,
            "text": text,
            "categories": sorted(classify_message_semantics(text)),
            "question_like": is_question_like(text),
        }
        for index, text in enumerate(texts)
    ]


def classify_message_semantics(text: str) -> set[str]:
    clean = normalize_semantic_text(text)
    categories: set[str] = set()
    if any(term in clean for term in ("预算", "万", "价格", "价位", "左右")):
        categories.add("budget_or_price")
    if any(term in clean for term in ("推荐", "怎么选", "适合", "家用", "通勤", "省油", "自动挡", "代步")):
        categories.add("selection_need")
    if any(term in clean for term in ("有车", "现车", "车源", "车型", "公里", "年份", "车况")):
        categories.add("vehicle_source")
    if any(term in clean for term in ("分期", "贷款", "首付", "月供", "金融")):
        categories.add("finance")
    if any(term in clean for term in APPOINTMENT_TERMS):
        categories.add("appointment")
    if any(term in clean for term in TRADE_IN_TERMS):
        categories.add("trade_in")
    if any(term in clean for term in DOCUMENT_TERMS):
        categories.add("document_boundary")
    if any(term in clean for term in INTERNAL_PROBE_TERMS):
        categories.add("identity_probe")
    if any(term in clean for term in OFF_TOPIC_TERMS):
        categories.add("off_topic")
    if any(term in clean for term in AFTER_SALES_TERMS):
        categories.add("after_sales_dispute")
    if any(term in clean for term in ("最低", "优惠", "折扣", "少点", "便宜点", "还能便宜")):
        categories.add("price_approval")
    if not categories:
        categories.add("general")
    return categories


def normalize_semantic_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").strip())


def is_question_like(text: str) -> bool:
    clean = str(text or "").strip()
    return any(marker in clean for marker in ("?", "？", "吗", "么", "怎么", "多少", "能不能", "有没有", "可不可以"))


def is_single_need_fragment_batch(segments: list[dict[str, Any]]) -> bool:
    if len(segments) <= 1:
        return True
    if has_stale_general_noise_before_business_turn(segments):
        return False
    categories = {category for segment in segments for category in segment.get("categories", [])}
    complementary = {"budget_or_price", "selection_need", "vehicle_source", "trade_in", "general"}
    if categories <= complementary:
        question_count = sum(1 for segment in segments if segment.get("question_like"))
        short_count = sum(1 for segment in segments if len(str(segment.get("text") or "")) <= 18)
        return question_count <= 1 or short_count >= max(1, len(segments) - 1)
    return False


def has_stale_general_noise_before_business_turn(segments: list[dict[str, Any]]) -> bool:
    """Detect old visible noise that should not be merged into a new need.

    OCR/RPA can see an unrelated long visible line above the latest customer
    message when the ledger anchor is unavailable.  Treat that as separate
    context rather than one customer need, so the Brain focuses on the latest
    actionable business turn while still receiving the raw segments for audit.
    """

    if len(segments) < 2:
        return False
    latest = segments[-1]
    latest_categories = set(latest.get("categories") or [])
    business_categories = {"budget_or_price", "selection_need", "vehicle_source", "finance", "appointment", "trade_in", "price_approval"}
    if not (latest_categories & business_categories):
        return False
    for segment in segments[:-1]:
        categories = set(segment.get("categories") or [])
        text = normalize_semantic_text(str(segment.get("text") or ""))
        if categories == {"general"} and len(text) >= 60:
            return True
    return False


def looks_like_spam_or_noise(texts: list[str], *, threshold: float) -> bool:
    if len(texts) < 8:
        return False
    normalized = [normalize_semantic_text(text) for text in texts if normalize_semantic_text(text)]
    if not normalized:
        return True
    unique_ratio = len(set(normalized)) / max(1, len(normalized))
    if unique_ratio <= max(0.1, 1.0 - threshold):
        return True
    short_noise = sum(1 for text in normalized if len(text) <= 3)
    return short_noise >= max(6, int(len(normalized) * 0.7))


def build_semantic_combined_text(kind: str, texts: list[str]) -> str:
    if kind == "single_event":
        return "客户连续补充同一个需求：\n" + "\n".join(f"- {text}" for text in texts)
    if kind == "multi_question_mixed_risk":
        return "客户连续发来多个问题，其中包含需要请示确认的边界问题：\n" + "\n".join(
            f"{index}. {text}" for index, text in enumerate(texts, 1)
        )
    if kind == "spam_or_noise":
        return "客户连续发送了多条重复或较碎的信息，请结合最新有效信息简短回应：\n" + "\n".join(
            f"- {text}" for text in texts[-8:]
        )
    return "客户连续问了几个相关问题：\n" + "\n".join(f"{index}. {text}" for index, text in enumerate(texts, 1))


def detect_newer_messages_before_send(
    *,
    connector: WeChatConnector,
    target: TargetConfig,
    target_state: dict[str, Any],
    batch: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prevent sending a stale LLM reply when the customer talks during thinking."""
    original_ids = [reply_input_message_identity(item) for item in batch if reply_input_message_identity(item)]
    original_content_keys: set[str] = set()
    for item in batch:
        original_content_keys.update(message_original_match_keys(item))
    if not original_ids:
        return {"ok": True, "has_newer_messages": False, "reason": "empty_batch"}
    payload = connector.get_messages(
        target.name,
        exact=target.exact,
        session_key=str(getattr(target, "session_key", "") or ""),
    )
    if not payload.get("ok"):
        return {"ok": False, "has_newer_messages": False, "reason": "latest_read_failed", "messages": payload}
    messages = payload.get("messages", []) or []
    history_meta: dict[str, Any] = {"applied": False}
    latest_original_index = -1
    original_set = set(original_ids)
    for index, message in enumerate(messages):
        if message_matches_original_batch(message, original_set, original_content_keys):
            latest_original_index = max(latest_original_index, index)
    if latest_original_index < 0:
        settings = history_backfill_settings(config)
        if settings.get("enabled") and str(settings.get("mode") or "").strip().lower() == "anchor_until_found":
            freshness_anchor_enabled = bool(settings.get("freshness_anchor_scroll_enabled"))
            max_scroll_steps = bounded_nonnegative_int(
                settings.get("freshness_anchor_max_scroll_steps"),
                default=0,
                maximum=bounded_nonnegative_int(settings.get("max_scroll_steps"), default=positive_int(settings.get("max_load_times"), 5), maximum=16),
            )
            if freshness_anchor_enabled and max_scroll_steps > 0:
                try:
                    loaded = connector.get_messages(
                        target.name,
                        exact=target.exact,
                        session_key=str(getattr(target, "session_key", "") or ""),
                        history_mode="anchor_until_found",
                        anchor_ids=original_ids,
                        anchor_content_keys=sorted(original_content_keys),
                        max_scroll_steps=max_scroll_steps,
                        max_duration_seconds=bounded_nonnegative_int(settings.get("max_duration_seconds"), default=12, maximum=60),
                        max_snapshots=bounded_nonnegative_int(settings.get("max_snapshots"), default=max_scroll_steps + 2, maximum=24),
                        min_delay_ms=bounded_nonnegative_int(settings.get("min_delay_ms"), default=180, maximum=5000),
                        max_delay_ms=bounded_nonnegative_int(settings.get("max_delay_ms"), default=650, maximum=10000),
                        restore_to_latest=bool(settings.get("restore_to_latest", True)),
                    )
                except Exception as exc:
                    loaded = {"ok": False, "error": repr(exc)}
                history_meta = {
                    "applied": bool(loaded.get("ok")),
                    "mechanism": str((loaded.get("history_load") or {}).get("mechanism") or "rpa.history_load"),
                    "mode": "anchor_until_found",
                    "max_scroll_steps": max_scroll_steps,
                    "result_ok": bool(loaded.get("ok")),
                    "error": loaded.get("error"),
                    "sidecar_history_load": loaded.get("history_load"),
                }
                if loaded.get("ok"):
                    messages = merge_message_windows(
                        loaded.get("messages", []) or [],
                        messages,
                        max_messages=positive_int(settings.get("max_messages_after_load"), 80),
                    )
                    latest_original_index = -1
                    for index, message in enumerate(messages):
                        if message_matches_original_batch(message, original_set, original_content_keys):
                            latest_original_index = max(latest_original_index, index)
            else:
                history_meta = {
                    "applied": False,
                    "mechanism": "rpa.history_load",
                    "mode": "anchor_until_found",
                    "max_scroll_steps": 0,
                    "result_ok": True,
                    "skip_reason": "freshness_anchor_scroll_disabled",
                }
        else:
            load_times = bounded_nonnegative_int(
                settings.get("freshness_load_times"),
                default=0,
                maximum=positive_int(settings.get("max_load_times"), 5),
            )
            if settings.get("enabled") and load_times > 0:
                try:
                    loaded = connector.get_messages(
                        target.name,
                        exact=target.exact,
                        history_load_times=load_times,
                        session_key=str(getattr(target, "session_key", "") or ""),
                    )
                except Exception as exc:
                    loaded = {"ok": False, "error": repr(exc)}
                history_meta = {
                    "applied": bool(loaded.get("ok")),
                    "mechanism": str((loaded.get("history_load") or {}).get("mechanism") or "rpa.history_load"),
                    "load_times": load_times,
                    "result_ok": bool(loaded.get("ok")),
                    "error": loaded.get("error"),
                    "sidecar_history_load": loaded.get("history_load"),
                }
                if loaded.get("ok"):
                    messages = merge_message_windows(
                        loaded.get("messages", []) or [],
                        messages,
                        max_messages=positive_int(settings.get("max_messages_after_load"), 80),
                    )
                    latest_original_index = -1
                    for index, message in enumerate(messages):
                        if message_matches_original_batch(message, original_set, original_content_keys):
                            latest_original_index = max(latest_original_index, index)
        if latest_original_index >= 0:
            history_meta["original_batch_found_after_history_load"] = True
        else:
            history_meta["original_batch_found_after_history_load"] = False
            if history_meta.get("skip_reason") == "freshness_anchor_scroll_disabled":
                history_meta["gap_risk"] = False
                history_meta["gap_reason"] = ""
            else:
                history_meta["gap_risk"] = True
                history_meta["gap_reason"] = "original_batch_missing_after_freshness_backfill"
    if latest_original_index < 0:
        processed = set(target_state.get("processed_message_ids", []))
        processed_content_keys = set(target_state.get("processed_content_keys", []))
        handoff = set(target_state.get("handoff_message_ids", []))
        sent_reply_content_keys = recent_sent_reply_content_keys(target_state)
        visible_unprocessed = [
            {
                "id": reply_input_message_identity(message),
                "content": str(message.get("content") or "").strip()[:220],
                "sender": str(message.get("sender") or ""),
            }
            for message in messages
            if message_is_reply_candidate(
                message,
                processed=processed,
                processed_content_keys=processed_content_keys,
                handoff=handoff,
                allow_self_for_test=target.allow_self_for_test,
                config=config,
                sent_reply_content_keys=sent_reply_content_keys,
            )
            and not message_matches_original_batch(message, original_set, original_content_keys)
        ]
        if target.allow_self_for_test:
            # File Transfer Assistant loopback keeps older self-sent synthetic
            # prompts visible for a long time. When the original batch is no
            # longer visible, those legacy self messages are not reliable proof
            # of a newer unread customer event, so ignore them in this
            # last-resort stale heuristic. Real newer loopback messages are
            # still caught by the ordered branch below when the original batch
            # remains visible.
            visible_unprocessed = [item for item in visible_unprocessed if str(item.get("sender") or "").strip().lower() != "self"]
        gap_risk = bool(history_meta.get("gap_risk"))
        return {
            "ok": True,
            "has_newer_messages": bool(visible_unprocessed) or gap_risk,
            "reason": (
                "original_batch_not_visible_assume_stale"
                if visible_unprocessed
                else "original_batch_not_found_gap_risk"
                if gap_risk
                else "original_batch_not_found_no_visible_unprocessed"
            ),
            "newer_message_ids": [item["id"] for item in visible_unprocessed],
            "newer_messages": visible_unprocessed[:5],
            "history_backfill": history_meta,
            "gap_risk": gap_risk,
        }
    processed = set(target_state.get("processed_message_ids", []))
    processed_content_keys = set(target_state.get("processed_content_keys", []))
    handoff = set(target_state.get("handoff_message_ids", []))
    sent_reply_content_keys = recent_sent_reply_content_keys(target_state)
    newer: list[dict[str, Any]] = []
    for message in messages[latest_original_index + 1 :]:
        if not message_is_reply_candidate(
            message,
            processed=processed,
            processed_content_keys=processed_content_keys,
            handoff=handoff,
            allow_self_for_test=target.allow_self_for_test,
            config=config,
            sent_reply_content_keys=sent_reply_content_keys,
        ):
            continue
        message_id = reply_input_message_identity(message)
        content = str(message.get("content") or "").strip()
        sender = str(message.get("sender") or "")
        if message_matches_original_batch(message, original_set, original_content_keys):
            continue
        newer.append({"id": message_id, "content": content[:220], "sender": sender})
    return {
        "ok": True,
        "has_newer_messages": bool(newer),
        "newer_message_ids": [item["id"] for item in newer],
        "newer_messages": newer[:5],
        "history_backfill": history_meta,
    }


def message_matches_original_batch(
    message: dict[str, Any],
    original_ids: set[str],
    original_content_keys: set[str],
) -> bool:
    message_id = reply_input_message_identity(message)
    if message_id and message_id in original_ids:
        return True
    if any(key in original_content_keys for key in message_original_match_keys(message)):
        return True
    return message_content_is_original_fragment(message, original_content_keys)


def message_content_is_original_fragment(message: dict[str, Any], original_content_keys: set[str]) -> bool:
    content = normalize_ocr_content_for_dedupe(str(message.get("content") or ""))
    if len(content) < 8:
        return False
    for key in original_content_keys:
        parts = str(key or "").split("\x1f")
        if len(parts) != 3:
            continue
        original = normalize_ocr_content_for_dedupe(parts[2])
        if len(original) < 8:
            continue
        if content in original or original in content:
            return True
    return False


def message_stable_content_key(message: dict[str, Any]) -> str:
    content = str(message.get("content") or "").strip()
    if not content:
        return ""
    return "\x1f".join(
        [
            str(message.get("sender") or "").strip(),
            str(message.get("type") or "").strip(),
            content,
        ]
    )


def customer_service_anchor_sets(target_state: dict[str, Any]) -> tuple[set[str], set[str]]:
    payload = customer_service_anchor_payload(target_state)
    return set(payload.get("anchor_ids", []) or []), set(payload.get("anchor_content_keys", []) or [])


def customer_service_anchor_payload(target_state: dict[str, Any]) -> dict[str, list[str]]:
    anchor_ids = {
        str(item).strip()
        for key in ("processed_message_ids", "handoff_message_ids")
        for item in target_state.get(key, []) or []
        if str(item).strip()
    }
    anchor_content_keys: set[str] = set()
    for item in target_state.get("processed_content_keys", []) or []:
        key = str(item).strip()
        if not key:
            continue
        if processed_content_key_is_repeatable_probe(key):
            continue
        anchor_content_keys.add(key)
        normalized_key = normalized_processed_content_key(key)
        if normalized_key:
            anchor_content_keys.add(normalized_key)
        content_only_key = normalized_content_only_processed_key(key)
        if content_only_key:
            anchor_content_keys.add(content_only_key)

    reply_content_keys: set[str] = set()
    last_anchor = target_state.get("last_successful_reply_anchor")
    if isinstance(last_anchor, dict):
        for item in last_anchor.get("message_ids", []) or []:
            value = str(item or "").strip()
            if value:
                anchor_ids.add(value)
        for item in last_anchor.get("message_content_keys", []) or []:
            value = str(item or "").strip()
            if not value:
                continue
            if processed_content_key_is_repeatable_probe(value):
                continue
            anchor_content_keys.add(value)
            normalized_value = normalized_processed_content_key(value)
            if normalized_value:
                anchor_content_keys.add(normalized_value)
            content_only_value = normalized_content_only_processed_key(value)
            if content_only_value:
                anchor_content_keys.add(content_only_value)
        reply_key = normalize_reply_content_key(str(last_anchor.get("reply_text") or last_anchor.get("reply_text_sample") or ""))
        if reply_key:
            reply_content_keys.add(reply_key)
        stored_reply_key = normalize_reply_content_key(str(last_anchor.get("reply_content_key") or ""))
        if stored_reply_key:
            reply_content_keys.add(stored_reply_key)

    for item in (target_state.get("sent_replies", []) or [])[-5:]:
        if not isinstance(item, dict):
            continue
        for message_id in item.get("message_ids", []) or []:
            value = str(message_id or "").strip()
            if value:
                anchor_ids.add(value)
        for content in item.get("message_contents", []) or []:
            mock_message = {"sender": "customer", "type": "text", "content": str(content or "")}
            for key in message_processed_content_keys(mock_message):
                anchor_content_keys.add(key)
                content_only_key = normalized_content_only_processed_key(key)
                if content_only_key:
                    anchor_content_keys.add(content_only_key)
        reply_key = normalize_reply_content_key(str(item.get("reply_text") or ""))
        if reply_key:
            reply_content_keys.add(reply_key)

    return {
        "anchor_ids": sorted(anchor_ids),
        "anchor_content_keys": sorted(anchor_content_keys),
        "reply_content_keys": sorted(reply_content_keys),
    }


def find_latest_customer_service_anchor_index(
    messages: list[dict[str, Any]],
    anchor_ids: set[str],
    anchor_content_keys: set[str],
    *,
    reply_content_keys: set[str] | None = None,
) -> int:
    reply_keys = reply_content_keys or set()
    if not anchor_ids and not anchor_content_keys and not reply_keys:
        return -1
    latest = -1
    for index, message in enumerate(messages or []):
        if not isinstance(message, dict):
            continue
        message_id = reply_input_message_identity(message)
        match_keys = message_anchor_match_keys(message)
        reply_key = normalize_reply_content_key(str(message.get("content") or ""))
        if (
            (message_id and message_id in anchor_ids)
            or any(key in anchor_content_keys for key in match_keys)
            or (reply_key and reply_key in reply_keys)
        ):
            latest = index
    return latest


def message_processed_content_keys(message: dict[str, Any]) -> list[str]:
    if message_has_repeatable_probe_content(message):
        return []
    keys: list[str] = []
    for key in (
        message_stable_content_key(message),
        message_content_dedupe_key(message),
        *message_fragment_content_keys(message),
    ):
        if key and key not in keys:
            keys.append(key)
    return keys


def message_original_match_keys(message: dict[str, Any]) -> list[str]:
    keys = message_processed_content_keys(message)
    for key in list(keys):
        parts = str(key or "").split("\x1f")
        if len(parts) != 3 or not parts[2]:
            continue
        content_only = "\x1f".join(["content", parts[2]])
        if content_only not in keys:
            keys.append(content_only)
    return keys


def message_anchor_match_keys(message: dict[str, Any]) -> list[str]:
    keys = message_processed_content_keys(message)
    for key in list(keys):
        content_only = normalized_content_only_processed_key(key)
        if content_only and content_only not in keys:
            keys.append(content_only)
    return keys


def message_fragment_content_keys(message: dict[str, Any]) -> list[str]:
    content = str(message.get("content") or "").strip()
    if not content:
        return []
    sender = str(message.get("sender") or "").strip()
    message_type = str(message.get("type") or "").strip()
    lines = [line.strip() for line in re.split(r"\n+", content) if line.strip()]
    fragments: list[str] = []
    for line in lines:
        normalized = normalize_ocr_content_for_dedupe(line)
        if len(normalized) >= 16:
            fragments.append(normalized)
    for index in range(len(lines) - 1):
        normalized = normalize_ocr_content_for_dedupe(lines[index] + lines[index + 1])
        if len(normalized) >= 24:
            fragments.append(normalized)
    keys: list[str] = []
    for fragment in fragments:
        key = "\x1f".join([sender, message_type, fragment])
        if key not in keys:
            keys.append(key)
    return keys


def normalized_processed_content_key(key: str) -> str:
    parts = str(key or "").split("\x1f")
    if len(parts) != 3:
        return ""
    content = normalize_ocr_content_for_dedupe(parts[2])
    if not content:
        return ""
    return "\x1f".join([parts[0], parts[1], content])


def normalized_content_only_processed_key(key: str, *, min_content_len: int = 6) -> str:
    parts = str(key or "").split("\x1f")
    if len(parts) != 3:
        return ""
    content = normalize_ocr_content_for_dedupe(parts[2])
    if len(content) < max(1, int(min_content_len or 1)):
        return ""
    return "\x1f".join(["content", content])


def append_unique_limited(values: list[str], keys: list[str]) -> None:
    for key in keys:
        if key and key not in values:
            values.append(key)


def history_backfill_gap_risk(
    *,
    settings: dict[str, Any],
    anchor_count: int,
    anchor_found: bool,
    selection: BatchSelection,
) -> bool:
    if not settings.get("gap_guard_enabled", True):
        return False
    if selection.eligible_count <= 0:
        return False
    if anchor_count > 0:
        return not anchor_found
    return bool(settings.get("first_window_gap_guard")) and selection.truncated


def history_gap_risk_blocks_reply(history_backfill: dict[str, Any], config: dict[str, Any] | None = None) -> bool:
    if not isinstance(history_backfill, dict) or not history_backfill.get("gap_risk"):
        return False
    if str(history_backfill.get("history_continuity") or "") == "overflow_unanchored":
        return False
    if history_backfill.get("overflow_batch") is True:
        return False
    settings = history_backfill_settings(config)
    return bool(settings.get("block_on_gap_risk", True))


def listener_runtime_status_after_result(result: dict[str, Any]) -> tuple[str, str]:
    events = [item for item in result.get("events", []) or [] if isinstance(item, dict)]
    last_event = events[-1] if events else {}
    if last_event.get("action") == "blocked" and last_event.get("reason") == "history_backfill_gap_risk":
        return "paused", "检测到微信消息窗口可能存在缺口，已暂停自动回复。"
    if any(item.get("action") == "pending_visible" for item in events):
        return "idle", BOOTSTRAP_PENDING_VISIBLE_MESSAGE
    return "idle", "本轮微信消息处理完成。"


def check_rate_limit(target_state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    limits = config.get("rate_limits", {}) or {}
    min_seconds = int(limits.get("min_seconds_between_replies", 0))
    max_per_10_minutes = int(limits.get("max_replies_per_10_minutes", 20))
    max_per_hour = int(limits.get("max_replies_per_hour", 100))
    now = datetime.now()
    timestamps = [
        parsed
        for parsed in (parse_datetime(item) for item in target_state.get("reply_timestamps", []))
        if parsed is not None
    ]
    blocks = []
    if timestamps and min_seconds > 0 and (now - max(timestamps)).total_seconds() < min_seconds:
        retry_after_at = max(timestamps) + timedelta(seconds=min_seconds)
        blocks.append(rate_limit_blocked("min_seconds_between_replies", now, retry_after_at))
    for reason, window_seconds, max_count in [
        ("max_replies_per_10_minutes", 10 * 60, max_per_10_minutes),
        ("max_replies_per_hour", 60 * 60, max_per_hour),
    ]:
        block = check_window_rate_limit(timestamps, now, reason, window_seconds, max_count)
        if block:
            blocks.append(block)
    if blocks:
        return max(blocks, key=lambda item: parse_datetime(str(item.get("retry_after_at") or "")) or now)
    return {"allowed": True, "reason": "ok"}


def check_window_rate_limit(
    timestamps: list[datetime],
    now: datetime,
    reason: str,
    window_seconds: int,
    max_count: int,
) -> dict[str, Any] | None:
    if max_count <= 0:
        return None
    window = timedelta(seconds=window_seconds)
    recent = sorted(item for item in timestamps if now - item <= window)
    if len(recent) < max_count:
        return None
    release_index = max(0, len(recent) - max_count)
    retry_after_at = recent[release_index] + window + timedelta(seconds=1)
    return rate_limit_blocked(reason, now, retry_after_at, window_seconds=window_seconds, max_count=max_count)


def rate_limit_blocked(
    reason: str,
    now: datetime,
    retry_after_at: datetime,
    window_seconds: int | None = None,
    max_count: int | None = None,
) -> dict[str, Any]:
    retry_after_seconds = max(1, int((retry_after_at - now).total_seconds()))
    payload = {
        "allowed": False,
        "reason": reason,
        "retry_after_at": retry_after_at.isoformat(timespec="seconds"),
        "retry_after_seconds": retry_after_seconds,
    }
    if window_seconds is not None:
        payload["window_seconds"] = window_seconds
    if max_count is not None:
        payload["max_count"] = max_count
    return payload


def get_rate_limit_backoff(target_state: dict[str, Any], message_ids: list[str]) -> dict[str, Any] | None:
    backoff = target_state.get("rate_limit_backoff")
    if not isinstance(backoff, dict):
        return None
    retry_after_at = parse_datetime(str(backoff.get("retry_after_at") or ""))
    if retry_after_at is None:
        return None
    if datetime.now() >= retry_after_at:
        target_state.pop("rate_limit_backoff", None)
        return None
    backoff["observed_message_ids"] = unique_list(
        [*[str(item) for item in backoff.get("observed_message_ids", [])], *message_ids]
    )
    return backoff


def record_rate_limit_backoff(
    target_state: dict[str, Any],
    message_ids: list[str],
    rate_check: dict[str, Any],
) -> None:
    existing = target_state.get("rate_limit_backoff")
    attempts = int(existing.get("attempts", 0)) + 1 if isinstance(existing, dict) else 1
    target_state["rate_limit_backoff"] = {
        "message_ids": message_ids,
        "observed_message_ids": message_ids,
        "reason": rate_check.get("reason"),
        "retry_after_at": rate_check.get("retry_after_at"),
        "retry_after_seconds": rate_check.get("retry_after_seconds"),
        "attempts": attempts,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def clear_rate_limit_backoff(target_state: dict[str, Any], message_ids: list[str]) -> None:
    backoff = target_state.get("rate_limit_backoff")
    if not isinstance(backoff, dict):
        return
    target_state.pop("rate_limit_backoff", None)


def should_send_rate_limit_notice(
    target_state: dict[str, Any],
    config: dict[str, Any],
    rate_check: dict[str, Any],
) -> bool:
    settings = config.get("rate_limits", {}) or {}
    if not settings.get("notice_customer", True):
        return False
    retry_after_at = str(rate_check.get("retry_after_at") or "")
    if not retry_after_at:
        return False
    notices = target_state.get("rate_limit_notices", []) or []
    if not notices:
        return True
    latest = notices[-1]
    if latest.get("retry_after_at") == retry_after_at:
        return False
    min_interval = int(settings.get("notice_min_interval_seconds", 300))
    latest_at = parse_datetime(str(latest.get("sent_at") or ""))
    if latest_at and (datetime.now() - latest_at).total_seconds() < min_interval:
        return False
    return True


def build_rate_limit_notice_text(config: dict[str, Any], rate_check: dict[str, Any]) -> str:
    settings = config.get("rate_limits", {}) or {}
    retry_after_seconds = int(rate_check.get("retry_after_seconds") or 60)
    retry_after_minutes = max(1, int((retry_after_seconds + 59) / 60))
    reason_label = {
        "min_seconds_between_replies": "回复过快",
        "max_replies_per_10_minutes": "10分钟回复额度",
        "max_replies_per_hour": "1小时回复额度",
    }.get(str(rate_check.get("reason") or ""), "回复额度")
    template = str(
        settings.get("notice_reply")
        or "当前消息较多，已达到{reason_label}上限，请稍等约 {retry_after_minutes} 分钟，我会继续处理您的消息。"
    )
    return template.format(
        reason_label=reason_label,
        retry_after_minutes=retry_after_minutes,
        retry_after_at=rate_check.get("retry_after_at") or "",
    )


def record_rate_limit_notice(
    target_state: dict[str, Any],
    message_ids: list[str],
    rate_check: dict[str, Any],
    notice_text: str,
) -> None:
    notices = list(target_state.get("rate_limit_notices", []) or [])
    notices.append(
        {
            "message_ids": message_ids,
            "reason": rate_check.get("reason"),
            "retry_after_at": rate_check.get("retry_after_at"),
            "notice_text": notice_text,
            "sent_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    target_state["rate_limit_notices"] = notices[-MAX_STORED_IDS:]


def record_reply_timestamp(target_state: dict[str, Any]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    timestamps = list(target_state.get("reply_timestamps", []))
    timestamps.append(now)
    target_state["reply_timestamps"] = timestamps[-MAX_STORED_IDS:]


def build_reply_trace_id(target_name: str, batch: list[dict[str, Any]], reply_text: str) -> str:
    message_ids = [reply_input_message_identity(item) for item in batch if reply_input_message_identity(item)]
    seed = json.dumps(
        {
            "target": target_name,
            "message_ids": message_ids,
            "reply_text": str(reply_text or ""),
            "question": [str(item.get("content") or "") for item in batch],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "reply_trace_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def mark_processed(
    target_state: dict[str, Any],
    batch: list[dict[str, Any]],
    reply_text: str,
    *,
    reply_trace_id: str = "",
    send_result: dict[str, Any] | None = None,
) -> None:
    processed = list(target_state.get("processed_message_ids", []))
    processed_content_keys = list(target_state.get("processed_content_keys", []))
    for message in batch:
        message_id = reply_input_message_identity(message)
        if message_id and message_id not in processed:
            processed.append(message_id)
        append_unique_limited(processed_content_keys, message_processed_content_keys(message))
    target_state["processed_message_ids"] = processed[-MAX_STORED_IDS:]
    target_state["processed_content_keys"] = processed_content_keys[-MAX_STORED_IDS:]
    entry = {
        "reply_trace_id": reply_trace_id or build_reply_trace_id("", batch, reply_text),
        "message_ids": [reply_input_message_identity(item) for item in batch if reply_input_message_identity(item)],
        "message_contents": [item.get("content") for item in batch],
        "reply_text": reply_text,
        "processed_at": datetime.now().isoformat(timespec="seconds"),
    }
    if send_result:
        entry["send_verified"] = bool(send_result.get("verified"))
    target_state.setdefault("sent_replies", []).append(entry)
    if send_result and bool(send_result.get("verified")):
        message_content_keys: list[str] = []
        for message in batch:
            append_unique_limited(message_content_keys, message_processed_content_keys(message))
        target_state["last_successful_reply_anchor"] = {
            "reply_trace_id": entry["reply_trace_id"],
            "message_ids": [reply_input_message_identity(item) for item in batch if reply_input_message_identity(item)],
            "message_content_keys": message_content_keys[-MAX_STORED_IDS:],
            "reply_content_key": normalize_reply_content_key(reply_text),
            "reply_text_sample": str(reply_text or "").strip()[:160],
            "processed_at": entry["processed_at"],
            "send_verified": True,
        }


def mark_coalesced_messages(
    target_state: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    reply_trace_id: str = "",
    reply_text: str = "",
    reason: str = "overflow_coalesced",
) -> None:
    if not messages:
        return
    processed = list(target_state.get("processed_message_ids", []))
    processed_content_keys = list(target_state.get("processed_content_keys", []))
    message_ids: list[str] = []
    for message in messages:
        message_id = reply_input_message_identity(message)
        if not message_id:
            continue
        message_ids.append(message_id)
        if message_id not in processed:
            processed.append(message_id)
        append_unique_limited(processed_content_keys, message_processed_content_keys(message))
    if not message_ids:
        return
    target_state["processed_message_ids"] = processed[-MAX_STORED_IDS:]
    target_state["processed_content_keys"] = processed_content_keys[-MAX_STORED_IDS:]
    target_state.setdefault("coalesced_message_batches", []).append(
        {
            "reply_trace_id": reply_trace_id,
            "message_ids": message_ids,
            "message_contents": [item.get("content") for item in messages],
            "reply_text": reply_text,
            "reason": reason,
            "processed_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    target_state["coalesced_message_batches"] = target_state["coalesced_message_batches"][-MAX_STORED_IDS:]


def mark_handoff(
    target_state: dict[str, Any],
    batch: list[dict[str, Any]],
    reason: str,
    status: str = "open",
    operator_alert: dict[str, Any] | None = None,
    reply_trace_id: str = "",
    reply_text: str = "",
) -> None:
    handoff = list(target_state.get("handoff_message_ids", []))
    processed_content_keys = list(target_state.get("processed_content_keys", []))
    for message in batch:
        message_id = reply_input_message_identity(message)
        if message_id and message_id not in handoff:
            handoff.append(message_id)
        append_unique_limited(processed_content_keys, message_processed_content_keys(message))
    target_state["handoff_message_ids"] = handoff[-MAX_STORED_IDS:]
    target_state["processed_content_keys"] = processed_content_keys[-MAX_STORED_IDS:]
    event = {
        "reply_trace_id": reply_trace_id or build_reply_trace_id("", batch, reply_text),
        "message_ids": [reply_input_message_identity(item) for item in batch if reply_input_message_identity(item)],
        "message_contents": [item.get("content") for item in batch],
        "reply_text": reply_text,
        "reason": reason,
        "status": status,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if operator_alert:
        event["operator_alert"] = operator_alert
    target_state.setdefault("handoff_events", []).append(event)


def base_event(target: TargetConfig, action: str, extra: dict[str, Any]) -> dict[str, Any]:
    event = {
        "ok": action != "error",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "target": target.name,
        "action": action,
    }
    event.update(extra)
    return event


def should_adopt_customer_service_brain(result: dict[str, Any] | None) -> bool:
    """Return whether the guarded Brain First reply should become the draft."""

    payload = result if isinstance(result, dict) else {}
    if str(payload.get("mode") or "") != "brain_first":
        return False
    if not payload.get("applied"):
        return False
    if not payload.get("adoptable"):
        return False
    if payload.get("customer_visible_reply_blocked"):
        return False
    owner = str(payload.get("visible_reply_owner") or "brain").strip()
    if owner and not owner.startswith("brain"):
        return False
    return bool(str(payload.get("raw_reply_text") or "").strip())


def customer_service_brain_controls_reply(result: dict[str, Any] | None) -> bool:
    """Brain First owns final reply authority; legacy generators are advisory only."""

    payload = result if isinstance(result, dict) else {}
    return bool(payload.get("enabled")) and str(payload.get("mode") or "") == "brain_first"


def brain_first_requires_brain_owned_visible_reply(config: dict[str, Any]) -> bool:
    """Return whether every customer-visible outbound text must be Brain-owned."""

    settings = effective_brain_settings(config)
    return (
        bool(settings.get("enabled", False))
        and str(settings.get("mode") or "") == "brain_first"
        and settings.get("fallback_to_legacy_on_error", False) is False
    )


def customer_visible_reply_is_brain_owned(
    *,
    event: dict[str, Any] | None = None,
    decision: dict[str, Any] | ReplyDecision | None = None,
) -> bool:
    """Check whether a ready customer-visible reply is owned by customer_service_brain."""

    event_payload = event if isinstance(event, dict) else {}
    if event_payload.get("customer_visible_reply_blocked"):
        return False
    if isinstance(decision, ReplyDecision):
        decision_payload = decision.__dict__
    else:
        decision_payload = decision if isinstance(decision, dict) else {}
    adopted = event_payload.get("customer_service_brain_adopted")
    adopted_payload = adopted if isinstance(adopted, dict) else {}
    brain = event_payload.get("customer_service_brain")
    brain_payload = brain if isinstance(brain, dict) else {}
    rule_name = str(
        decision_payload.get("rule_name")
        or adopted_payload.get("rule_name")
        or brain_payload.get("rule_name")
        or ""
    ).strip()
    if rule_name in {"customer_service_brain_no_visible_reply", "customer_service_brain_safe_fallback"}:
        return False
    if not rule_name.startswith("customer_service_brain_"):
        return False
    if adopted_payload:
        return bool(adopted_payload.get("applied"))
    owner = str(brain_payload.get("visible_reply_owner") or decision_payload.get("visible_reply_owner") or "").strip()
    if owner and owner.startswith(("none", "legacy", "guard", "quality_gate")):
        return False
    return True


def normalize_brain_owned_customer_visible_reply_text(reply_text: str, *, config: dict[str, Any]) -> str:
    """Only allow surface-noise cleanup on Brain-owned visible replies."""

    prefix, body = split_reply_prefix(reply_text, config)
    if not body:
        return str(reply_text or "").strip()
    cleaned_body = normalize_visible_reply_surface_noise(body, evidence_pack={})
    if prefix:
        return format_reply(cleaned_body, prefix)
    return cleaned_body


def config_with_legacy_reply_generators_disabled_for_brain(config: dict[str, Any]) -> dict[str, Any]:
    """Disable legacy reply generators while Brain First owns the visible reply."""

    next_config = copy.deepcopy(config)
    rag = dict(next_config.get("rag_response", {}) or {})
    rag["enabled"] = False
    next_config["rag_response"] = rag
    realtime = dict(next_config.get("realtime_reply", {}) or {})
    realtime["enabled"] = False
    next_config["realtime_reply"] = realtime
    synthesis = dict(next_config.get("llm_reply_synthesis", {}) or {})
    synthesis["enabled"] = False
    next_config["llm_reply_synthesis"] = synthesis
    return next_config


def build_brain_first_reply_audit(
    event: dict[str, Any],
    *,
    reply_text: str = "",
    final_polish: dict[str, Any] | None = None,
) -> dict[str, Any]:
    brain = event.get("customer_service_brain") if isinstance(event.get("customer_service_brain"), dict) else {}
    adopted = event.get("customer_service_brain_adopted") if isinstance(event.get("customer_service_brain_adopted"), dict) else {}
    legacy = event.get("customer_service_brain_legacy_generators") if isinstance(event.get("customer_service_brain_legacy_generators"), dict) else {}
    decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
    final_payload = final_polish if isinstance(final_polish, dict) else event.get("final_visible_llm_polish")
    if not isinstance(final_payload, dict):
        final_payload = {}
    rule_name = str(brain.get("rule_name") or decision.get("rule_name") or "")
    visible_owner = str(brain.get("visible_reply_owner") or "").strip()
    if visible_owner:
        owner = visible_owner
    elif bool(adopted.get("applied")):
        owner = "brain"
    elif brain.get("enabled") and str(brain.get("mode") or "") == "brain_first":
        owner = "brain_not_adopted"
    else:
        owner = "legacy_or_not_adopted"
    return {
        "reply_owner": owner,
        "brain_enabled": bool(brain.get("enabled")),
        "brain_mode": str(brain.get("mode") or ""),
        "brain_adopted": bool(adopted.get("applied")),
        "brain_reason": str(brain.get("reason") or adopted.get("reason") or ""),
        "brain_rule_name": rule_name,
        "visible_reply_owner": visible_owner or owner,
        "visible_reply_source": str(brain.get("visible_reply_source") or ""),
        "guard_verdict": str(brain.get("guard_verdict") or ""),
        "guard_hard_boundary": bool(brain.get("guard_hard_boundary", False)),
        "guard_reply_ignored": bool(brain.get("guard_reply_ignored", False)),
        "legacy_generators_disabled": bool(legacy.get("disabled")),
        "final_polish_enabled": bool(final_payload.get("enabled", False)),
        "final_polish_required": bool(final_payload.get("required", False)),
        "final_polish_changed": bool(final_payload.get("applied", False)),
        "final_polish_reason": str(final_payload.get("reason") or ""),
        "authority_sources": brain.get("authority_sources", {}),
        "visible_reply_chars": len(str(reply_text or "")),
    }


LEADING_CUSTOMER_GREETING_RE = re.compile(
    r"^\s*((?:[\u4e00-\u9fff]{1,2}(?:先生您好|女士您好|哥|姐|先生|女士))|哥|姐|老板|老师|您好|你好|亲|Hi|Hello)[，,]\s*(.*)$",
    re.I | re.S,
)
RECENT_CUSTOMER_GREETING_RE = re.compile(
    r"(?:^|[\s。！？!?，,])(?:[\u4e00-\u9fff]{0,2}(?:先生您好|女士您好|哥|姐|先生|女士)|哥|姐|老板|老师|您好|你好)[，,]"
)
NEW_TOPIC_GREETING_TERMS = (
    "另外",
    "还有",
    "再问",
    "再咨询",
    "换个",
    "换一台",
    "顺便",
    "那贷款",
    "那保险",
    "那过户",
    "合同",
    "发票",
    "置换",
    "试驾",
    "到店",
)
FOLLOWUP_LIKE_GREETING_OPENERS = (
    "那",
    "如果",
    "这",
    "这个",
    "这台",
    "刚才",
    "还有",
    "我还有",
    "最后",
    "行",
    "你先",
    "你别",
    "要是",
)
INITIAL_GREETING_LEAD_TERMS = (
    "你好",
    "您好",
    "在吗",
    "有人吗",
    "我想",
    "想买",
    "买台",
    "买辆",
    "帮我看",
    "推荐",
    "预算",
    "家用",
    "通勤",
    "练手",
)
NON_PERSON_DISPLAY_NAME_TERMS = (
    FILE_TRANSFER_ASSISTANT,
    "文件传输",
    "助手",
    "客户",
    "用户",
    "账号",
    "客服",
    "机器人",
    "测试",
    "长测",
    "群",
    "群聊",
    "车行",
    "二手车",
    "汽车",
    "公司",
    "门店",
    "销售",
    "顾问",
)
COMMON_COMPOUND_SURNAMES = (
    "欧阳",
    "上官",
    "司马",
    "诸葛",
    "东方",
    "南宫",
    "夏侯",
    "皇甫",
    "尉迟",
    "公孙",
    "慕容",
    "司徒",
    "令狐",
)

WEAK_VISIBLE_PRODUCT_ALIAS_TERMS = {
    "省油",
    "好开",
    "家用",
    "接娃",
    "通勤",
    "空间",
    "大空间",
    "空间大",
    "动力",
    "好停",
    "小巧",
    "混动",
    "新能源",
    "燃油",
    "自动挡",
    "手动挡",
    "豪华",
    "面子",
    "品质",
    "质感",
    "舒适",
    "商务",
    "七座",
    "7座",
    "南京",
    "现车",
    "本地",
    "一手车",
}


def _apply_greeting(
    reply_text: str,
    profile: dict[str, Any] | None,
    config: dict[str, Any],
    *,
    target_state: dict[str, Any] | None = None,
    combined: str = "",
    recent_reply_texts: list[str] | None = None,
) -> str:
    """Apply human-like customer honorifics only when the dialogue stage needs one."""
    text = str(reply_text or "").strip()
    if not text or not profile:
        return reply_text
    cfg = config.get("customer_profiles") or {}
    greeting_cfg = cfg.get("greeting") if isinstance(cfg, dict) else {}
    if not greeting_cfg or not greeting_cfg.get("enabled", True):
        return reply_text

    recent = list(recent_reply_texts or recent_customer_visible_reply_texts(target_state or {}))
    existing_greeting, body = _split_leading_customer_greeting(text)
    body = body.strip() if body else text
    should_use = _should_use_contextual_greeting(
        reply_text=body,
        combined=combined,
        recent_reply_texts=recent,
    )

    if existing_greeting and not should_use:
        return body

    if not should_use:
        return text

    selected = _select_contextual_honorific(
        profile=profile,
        combined=combined,
        reply_text=body,
        recent_reply_texts=recent,
    )
    if not selected:
        return text
    if existing_greeting:
        return f"{selected}，{body}"
    return f"{selected}，{text}"


def _split_leading_customer_greeting(text: str) -> tuple[str, str]:
    match = LEADING_CUSTOMER_GREETING_RE.match(str(text or "").strip())
    if not match:
        return "", str(text or "").strip()
    return str(match.group(1) or "").strip(), str(match.group(2) or "").strip()


def _should_use_contextual_greeting(
    *,
    reply_text: str,
    combined: str,
    recent_reply_texts: list[str],
) -> bool:
    if not recent_reply_texts:
        return _looks_like_initial_greeting_context(combined)
    if _recent_has_customer_greeting(recent_reply_texts, limit=4):
        return False
    if not _looks_like_new_topic(combined):
        return False
    seed = f"{combined}|{reply_text}|contextual-greeting"
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:2], 16) % 3 == 0


def _recent_has_customer_greeting(recent_reply_texts: list[str], *, limit: int = 4) -> bool:
    recent = " ".join(str(item or "") for item in recent_reply_texts[-max(1, limit) :])
    return bool(RECENT_CUSTOMER_GREETING_RE.search(recent))


def _looks_like_new_topic(text: str) -> bool:
    clean = re.sub(r"\s+", "", str(text or ""))
    return any(term in clean for term in NEW_TOPIC_GREETING_TERMS)


def _looks_like_initial_greeting_context(text: str) -> bool:
    clean = re.sub(r"\s+", "", str(text or ""))
    if not clean:
        return False
    if any(clean.startswith(term) for term in FOLLOWUP_LIKE_GREETING_OPENERS):
        return False
    return any(term in clean for term in INITIAL_GREETING_LEAD_TERMS)


def _select_contextual_honorific(
    *,
    profile: dict[str, Any],
    combined: str,
    reply_text: str,
    recent_reply_texts: list[str],
) -> str:
    basic = profile.get("basic_info") if isinstance(profile.get("basic_info"), dict) else {}
    gender = str(basic.get("gender") or "").strip().lower()
    try:
        confidence = float(basic.get("gender_confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    surname = _safe_customer_surname(profile)

    candidates: list[str] = []
    if confidence >= 0.7 and gender == "male":
        if surname:
            candidates.append(f"{surname}哥")
        candidates.extend(["哥", "老板"])
    elif confidence >= 0.7 and gender == "female":
        if surname:
            candidates.append(f"{surname}姐")
        candidates.append("姐")
    elif confidence >= 0.5:
        candidates.append("您好")

    deduped = list(dict.fromkeys(item for item in candidates if item))
    if not deduped:
        return ""
    selected, _ = choose_reply_variant(
        deduped,
        key_text=f"{combined}|{reply_text}|honorific",
        recent_reply_texts=recent_reply_texts,
    )
    return selected


def _safe_customer_surname(profile: dict[str, Any]) -> str:
    display_name = str(profile.get("display_name") or profile.get("target_name") or "").strip()
    if not display_name:
        return ""
    if any(term and term in display_name for term in NON_PERSON_DISPLAY_NAME_TERMS):
        return ""
    name = re.sub(r"[\s_@#\-—~·.。]+", "", display_name)
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", name):
        return ""
    for surname in COMMON_COMPOUND_SURNAMES:
        if name.startswith(surname):
            return surname
    if len(name) > 3:
        return ""
    return name[0]


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "targets": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_audit(path: Path, event: dict[str, Any]) -> None:
    append_jsonl(path, event)


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False))
        handle.write("\n")


def default_max_batch_messages(config: dict[str, Any] | None = None) -> int:
    cfg = config if isinstance(config, dict) else {}
    batching = cfg.get("batching", {}) if isinstance(cfg.get("batching"), dict) else {}
    multi_target = cfg.get("multi_target", {}) if isinstance(cfg.get("multi_target"), dict) else {}
    for value in (
        batching.get("max_batch_messages"),
        multi_target.get("default_max_batch_messages"),
        DEFAULT_MAX_BATCH_MESSAGES,
    ):
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return DEFAULT_MAX_BATCH_MESSAGES


def parse_targets(config: dict[str, Any], *, allow_empty: bool = False) -> list[TargetConfig]:
    targets = []
    default_batch = default_max_batch_messages(config)
    for item in config.get("targets", []) or []:
        if not item.get("enabled", False):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        allow_self = bool(item.get("allow_self_for_test", False))
        if allow_self and name != FILE_TRANSFER_ASSISTANT:
            raise ValueError("allow_self_for_test is only allowed for File Transfer Assistant")
        targets.append(
            TargetConfig(
                name=name,
                enabled=True,
                exact=bool(item.get("exact", True)),
                allow_self_for_test=allow_self,
                max_batch_messages=int(item.get("max_batch_messages", default_batch) or default_batch),
            )
        )
    if not targets and not allow_empty:
        raise ValueError("No enabled targets in config")
    return targets


def parse_runtime_targets(values: list[str], config_targets: list[TargetConfig]) -> list[TargetConfig]:
    config_by_name = {target.name: target for target in config_targets}
    targets = []
    for value in values:
        name = str(value or "").strip()
        if not name:
            continue
        if name in config_by_name:
            targets.append(config_by_name[name])
            continue
        targets.append(
            TargetConfig(
                name=name,
                enabled=True,
                exact=True,
                allow_self_for_test=False,
                max_batch_messages=DEFAULT_MAX_BATCH_MESSAGES,
            )
        )
    if not targets:
        raise ValueError("No runtime targets were provided")
    return targets


def build_iteration_targets(
    *,
    config_targets: list[TargetConfig],
    active_targets: list[Any],
    multi_target_cfg: dict[str, Any] | None,
    allow_dynamic_active_targets: bool = False,
    blocked_names: set[str] | None = None,
) -> list[TargetConfig]:
    """Build per-iteration target list: active sessions first, then full whitelist scan."""
    cfg = multi_target_cfg or {}
    prioritize_active = cfg.get("prioritize_active_sessions", True) is not False
    # RPA low-risk mode: prioritize unread/changed sessions and avoid
    # aggressive full-whitelist scans unless explicitly requested.
    rpa_low_risk_mode = cfg.get("rpa_low_risk_mode", True) is not False
    scan_all_raw = cfg.get("scan_all_whitelist_each_iteration")
    if scan_all_raw is None:
        scan_all = not rpa_low_risk_mode
    else:
        scan_all = scan_all_raw is not False
    active_limit_default = 3 if rpa_low_risk_mode else 5
    scan_limit_default = 3 if (rpa_low_risk_mode and scan_all) else 0
    idle_sweep_default = 0 if rpa_low_risk_mode else 1
    try:
        active_limit = int(cfg.get("max_targets_per_iteration", active_limit_default) or active_limit_default)
    except (TypeError, ValueError):
        active_limit = active_limit_default
    try:
        scan_limit = int(cfg.get("max_scan_targets_per_iteration", scan_limit_default) or scan_limit_default)
    except (TypeError, ValueError):
        scan_limit = scan_limit_default
    try:
        idle_whitelist_sweep_count = int(cfg.get("idle_whitelist_sweep_count", idle_sweep_default) or idle_sweep_default)
    except (TypeError, ValueError):
        idle_whitelist_sweep_count = idle_sweep_default
    try:
        dynamic_max_batch = int(
            cfg.get("dynamic_max_batch_messages")
            or cfg.get("default_max_batch_messages")
            or DEFAULT_MAX_BATCH_MESSAGES
        )
    except (TypeError, ValueError):
        dynamic_max_batch = DEFAULT_MAX_BATCH_MESSAGES

    config_by_name = {target.name: target for target in config_targets}
    ordered: list[TargetConfig] = []
    seen: set[str] = set()
    blocked = blocked_names or set()

    def push(target: TargetConfig) -> None:
        if target.name in blocked:
            return
        if target.name in seen:
            return
        seen.add(target.name)
        ordered.append(target)

    active_candidates = list(active_targets or [])
    if active_limit > 0:
        active_candidates = active_candidates[:active_limit]

    if prioritize_active:
        for item in active_candidates:
            name = str(getattr(item, "name", "") or "")
            base = config_by_name.get(name)
            if base is not None:
                push(base)
                continue
            if allow_dynamic_active_targets and name and name not in blocked:
                push(
                    TargetConfig(
                        name=name,
                        enabled=True,
                        exact=True,
                        allow_self_for_test=False,
                        max_batch_messages=max(1, dynamic_max_batch),
                    )
                )

    if scan_all:
        for base in config_targets:
            push(base)
    elif not prioritize_active:
        for item in active_candidates:
            name = str(getattr(item, "name", "") or "")
            base = config_by_name.get(name)
            if base is not None:
                push(base)
                continue
            if allow_dynamic_active_targets and name and name not in blocked:
                push(
                    TargetConfig(
                        name=name,
                        enabled=True,
                        exact=True,
                        allow_self_for_test=False,
                        max_batch_messages=max(1, dynamic_max_batch),
                    )
                )

    if not ordered and scan_all:
        for base in config_targets:
            push(base)
    elif not ordered and idle_whitelist_sweep_count > 0:
        for base in config_targets[:idle_whitelist_sweep_count]:
            push(base)

    if scan_limit > 0:
        return ordered[:scan_limit]
    return ordered


def multi_target_change_warmup_delay_seconds(multi_target_cfg: dict[str, Any] | None) -> float:
    """Short passive debounce after a session-list change to coalesce human bursts."""
    cfg = multi_target_cfg or {}
    if cfg.get("change_warmup_enabled", True) is False:
        return 0.0
    try:
        min_seconds = float(cfg.get("change_warmup_min_seconds", 0.8))
    except (TypeError, ValueError):
        min_seconds = 0.8
    try:
        max_seconds = float(cfg.get("change_warmup_max_seconds", 2.0))
    except (TypeError, ValueError):
        max_seconds = 2.0
    min_seconds = max(0.0, min(min_seconds, 5.0))
    max_seconds = max(min_seconds, min(max_seconds, 5.0))
    if max_seconds <= 0:
        return 0.0
    if max_seconds == min_seconds:
        return min_seconds
    return random.uniform(min_seconds, max_seconds)


def coalesce_active_targets(*target_groups: list[Any]) -> list[Any]:
    """Merge active target polls while preserving priority and first-seen order."""
    by_name: dict[str, Any] = {}
    order: dict[str, int] = {}
    counter = 0
    for group in target_groups:
        for item in group or []:
            name = str(getattr(item, "name", "") or "").strip()
            if not name:
                continue
            if name not in order:
                order[name] = counter
                counter += 1
            existing = by_name.get(name)
            if existing is None:
                by_name[name] = item
                continue
            current_score = int(getattr(item, "priority_score", 0) or 0)
            existing_score = int(getattr(existing, "priority_score", 0) or 0)
            current_age = int(getattr(item, "session_age_seconds", 0) or 0)
            existing_age = int(getattr(existing, "session_age_seconds", 0) or 0)
            if (current_score, current_age) > (existing_score, existing_age):
                by_name[name] = item
    return sorted(
        by_name.values(),
        key=lambda item: (
            -int(getattr(item, "priority_score", 0) or 0),
            -int(getattr(item, "session_age_seconds", 0) or 0),
            order.get(str(getattr(item, "name", "") or ""), 0),
        ),
    )


def resolve_iterations(args: argparse.Namespace, config: dict[str, Any]) -> int:
    if args.once:
        return 1
    if args.iterations is not None:
        return max(1, args.iterations)
    return max(1, int(config.get("poll", {}).get("iterations", 1)))


def resolve_path(value: Any) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path
    return ROOT / path


def parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def print_json(payload: dict[str, Any]) -> None:
    # stdout is parsed by the managed listener across a Windows process boundary.
    # Escape non-ASCII so Chinese target names survive regardless of console code page.
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
