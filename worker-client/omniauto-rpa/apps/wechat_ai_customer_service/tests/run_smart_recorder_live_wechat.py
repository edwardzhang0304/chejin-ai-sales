"""Live WeChat end-to-end checks for the AI smart recorder.

By default, this script prefers replaying historical live samples for the
target tenant and keeps recorder capture focused on one selected chat to avoid
unnecessary cross-session switching during RPA runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters.wechat_connector import FILE_TRANSFER_ASSISTANT, WeChatConnector  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.recorder_module_registry import RecorderModuleRegistryService  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.recorder_service import (  # noqa: E402
    RECORDER_DISCOVERY_SOURCE_TYPE,
    RecorderService,
)
from apps.wechat_ai_customer_service.admin_backend.services.raw_message_store import RawMessageStore  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import (  # noqa: E402
    tenant_context,
    tenant_industry_hint,
    tenant_runtime_root,
    tenant_review_candidates_root,
)
from apps.wechat_ai_customer_service.workflows.rag_layer import RagService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--group-name", default="新数据测试")
    parser.add_argument("--file-target", default=FILE_TRANSFER_ASSISTANT)
    parser.add_argument(
        "--message-source",
        choices=["auto", "historical", "synthetic"],
        default="auto",
        help="Message source mode. auto prefers tenant historical live samples when available.",
    )
    parser.add_argument("--history-sample-size", type=int, default=4, help="How many historical samples to send in one run.")
    parser.add_argument("--history-artifact", default="", help="Optional explicit path to a live_sim_*.json artifact.")
    parser.add_argument("--dual-target", action="store_true", help="Also send/capture file-target messages (legacy dual-chat mode).")
    parser.add_argument(
        "--preserve-selection",
        action="store_true",
        help="Keep existing selected chats untouched (legacy behavior).",
    )
    parser.add_argument("--skip-send", action="store_true", help="Only capture and verify existing live test messages.")
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM-assisted extraction for this live run.")
    parser.add_argument("--settle-seconds", type=float, default=1.8)
    args = parser.parse_args()

    with tenant_context(args.tenant):
        result = run_live_check(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def run_live_check(args: argparse.Namespace) -> dict[str, Any]:
    batch_token = "LIVE_RECORDER_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    tenant_id = str(args.tenant or "default")
    industry_hint = str(tenant_industry_hint(tenant_id) or "").strip().lower()
    industry_id = industry_hint or infer_industry_from_historical_artifact(tenant_id=tenant_id, history_artifact=args.history_artifact) or "home_appliance"
    connector = WeChatConnector()
    status = connector.status()
    if not status.get("ok") or not status.get("online"):
        return {"ok": False, "phase": "wechat_status", "status": status}

    service = RecorderService()
    raw_store = RawMessageStore()
    binding_bootstrap = ensure_live_test_module_binding(tenant_id=tenant_id, industry_id=industry_id)
    service.save_settings(
        {
            "group_recording_enabled": True,
            "private_recording_enabled": True,
            "file_transfer_recording_enabled": True,
            "auto_learn": True,
            "use_llm": not args.no_llm,
            "notify_on_collect": False,
        }
    )
    include_file_target = bool(args.dual_target)
    group = service.ensure_conversation(
        {
            "target_name": args.group_name,
            "display_name": args.group_name,
            "conversation_type": "group",
            "selected_by_user": True,
            "status": "active",
            "exact": True,
            "learning_enabled": True,
            "notify_enabled": False,
            "source": {"type": RECORDER_DISCOVERY_SOURCE_TYPE, "batch_token": batch_token, "origin": "live_recorder_test"},
        }
    )
    file_target = service.ensure_conversation(
        {
            "target_name": args.file_target,
            "display_name": args.file_target,
            "conversation_type": "file_transfer",
            "selected_by_user": include_file_target,
            "status": "active",
            "exact": True,
            "learning_enabled": True,
            "notify_enabled": False,
            "source": {"type": RECORDER_DISCOVERY_SOURCE_TYPE, "batch_token": batch_token, "origin": "live_recorder_test"},
        }
    )
    active_targets = [group, file_target] if include_file_target else [group]
    if not args.preserve_selection:
        enforce_selected_targets(service, [str(item.get("target_name") or "").strip() for item in active_targets])

    baseline_results = [baseline_conversation(connector, raw_store, conversation) for conversation in active_targets]
    if any(not item.get("ok") for item in baseline_results):
        return {"ok": False, "phase": "baseline", "batch_token": batch_token, "baseline": baseline_results}

    message_plan = build_live_message_plan(
        group_name=args.group_name,
        file_target=args.file_target,
        batch_token=batch_token,
        industry_id=industry_id,
        tenant_id=tenant_id,
        source_mode=str(args.message_source or "auto"),
        history_sample_size=int(args.history_sample_size or 4),
        history_artifact=str(args.history_artifact or ""),
        include_file_target=include_file_target,
    )
    expected = [str(item.get("token") or "").strip() for item in message_plan.get("entries", []) if str(item.get("token") or "").strip()]
    required_tokens = {
        str(item.get("token") or "").strip()
        for item in message_plan.get("entries", [])
        if str(item.get("token") or "").strip() and item.get("required", True) is not False
    }
    rag_required_tokens = required_tokens if str(message_plan.get("mode") or "") == "synthetic" else set()
    if not args.skip_send and not message_plan.get("entries"):
        return {
            "ok": False,
            "phase": "message_plan",
            "batch_token": batch_token,
            "tenant_id": tenant_id,
            "industry_id": industry_id,
            "message_plan": message_plan,
            "error": "no_live_messages_planned",
        }

    sent: list[dict[str, Any]] = []
    capture_history: list[dict[str, Any]] = []
    if not args.skip_send:
        for entry in message_plan.get("entries", []):
            target = str(entry.get("target") or "").strip()
            text = str(entry.get("text") or "")
            token = str(entry.get("token") or token_from_text(text))
            send_result = connector.send_text(target, text, exact=True)
            sent.append({"target": target, "ok": bool(send_result.get("ok")), "result": send_result, "token": token})
            time.sleep(max(0.5, args.settle_seconds))
            if not send_result.get("ok"):
                return {"ok": False, "phase": "send", "batch_token": batch_token, "sent": sent}
            capture_history.append(
                {
                    "target": target,
                    "token": token,
                    "capture": retry_capture_until_tokens(service, raw_store, [token], attempts=5, settle_seconds=args.settle_seconds),
                }
            )

    capture = retry_capture_until_tokens(service, raw_store, expected, attempts=3, settle_seconds=args.settle_seconds) if expected else {
        "ok": True,
        "captures": [],
        "raw_checks": [],
    }
    raw_checks = verify_raw_tokens(raw_store, expected)
    rag_checks = verify_rag_tokens(expected, required_tokens=rag_required_tokens)
    if str(message_plan.get("mode") or "") == "synthetic":
        candidate_checks = verify_candidate_expectations(batch_token)
    else:
        candidate_checks = {
            "skipped": True,
            "mode": str(message_plan.get("mode") or "historical"),
            "reason": "historical replay mode does not enforce synthetic candidate token expectations",
        }
    idempotency = verify_token_idempotency(service, raw_store, expected) if expected else {"ok": True, "capture": {}, "token_checks": []}

    failures = []
    if not all(item.get("found") for item in raw_checks):
        failures.append("raw_missing_tokens")
    if not all(item.get("found") for item in rag_checks if item.get("required", True)):
        failures.append("rag_missing_tokens")
    # Raw WeChat messages create RAG experiences only; candidates require manual promotion
    # We verify that noise does NOT create candidates, but do not expect auto-candidates from raw messages
    if not candidate_checks.get("skipped") and candidate_checks.get("noise_candidate_found"):
        failures.append("noise_created_candidate")
    if not idempotency.get("ok"):
        failures.append("idempotency_inserted_again")

    return {
        "ok": not failures,
        "batch_token": batch_token,
        "tenant_id": tenant_id,
        "industry_id": industry_id,
        "module_binding_bootstrap": binding_bootstrap,
        "targets": {"group": group, "file_transfer": file_target, "active_capture_targets": active_targets},
        "message_plan": message_plan,
        "sent": sent,
        "baseline": baseline_results,
        "capture_history": capture_history,
        "capture": capture,
        "raw_checks": raw_checks,
        "rag_checks": rag_checks,
        "candidate_checks": candidate_checks,
        "idempotency": idempotency,
        "failures": failures,
    }


def build_live_message_plan(
    *,
    group_name: str,
    file_target: str,
    batch_token: str,
    industry_id: str,
    tenant_id: str,
    source_mode: str,
    history_sample_size: int,
    history_artifact: str,
    include_file_target: bool,
) -> dict[str, Any]:
    requested_mode = str(source_mode or "auto").strip().lower()
    mode = requested_mode if requested_mode in {"historical", "synthetic"} else "auto"
    if mode in {"auto", "historical"}:
        historical = historical_replay_messages(
            tenant_id=tenant_id,
            group_name=group_name,
            batch_token=batch_token,
            sample_size=history_sample_size,
            explicit_artifact=history_artifact,
        )
        if historical:
            return {
                "mode": "historical",
                "requested_mode": requested_mode,
                "source_file": str(historical.get("source_file") or ""),
                "sample_size": len(historical.get("entries", [])),
                "entries": historical.get("entries", []),
            }
        if mode == "historical":
            return {
                "mode": "historical",
                "requested_mode": requested_mode,
                "source_file": str(history_artifact or ""),
                "sample_size": 0,
                "entries": [],
                "warning": "historical_source_not_found_or_empty",
            }

    synthetic_entries = synthetic_live_messages(group_name, file_target, batch_token, industry_id=industry_id)
    if not include_file_target:
        synthetic_entries = [item for item in synthetic_entries if item[0] == group_name]
    entries = [{"target": target, "text": text, "token": token_from_text(text), "required": True} for target, text in synthetic_entries]
    return {
        "mode": "synthetic",
        "requested_mode": requested_mode,
        "sample_size": len(entries),
        "entries": entries,
    }


def synthetic_live_messages(group_name: str, file_target: str, batch_token: str, *, industry_id: str) -> list[tuple[str, str]]:
    normalized = str(industry_id or "").strip().lower()
    if normalized == "lab_instruments":
        return [
            (
                group_name,
                "\n".join(
                    [
                        f"商品资料：记录员正常产品 {batch_token}",
                        f"测试批次：{batch_token}",
                        "商品名称：旋片式真空泵 2XZ-4",
                        "型号：LAB-LIVE-2XZ4",
                        "商品类目：实验仪器/真空设备",
                        "价格：2280",
                        "单位：台",
                        "库存：6",
                        "发货：江苏仓48小时内发出",
                        "售后：电机与泵体质保12个月，易损件除外",
                    ]
                ),
            ),
            (
                group_name,
                "\n".join(
                    [
                        f"政策规则：记录员缺字段边界 {batch_token}",
                        f"测试批次：{batch_token}",
                        "规则名称：危化品试剂采购转人工",
                        "规则类型：contract",
                        f"触发关键词：危化,易制毒,危化证,采购资质,{batch_token}",
                    ]
                ),
            ),
            (
                group_name,
                f"边界噪音：{batch_token}_NOISE 今天天气和测试心跳记录，不是产品资料，也不是客服知识。",
            ),
            (
                file_target,
                "\n".join(
                    [
                        f"聊天记录：记录员文件传输助手话术 {batch_token}",
                        f"客户：我想配一台常规细胞培养用真空泵，预算3000内。批次 {batch_token}",
                        "客服：可先看2XZ-4旋片泵，常规抽滤和负压场景都能覆盖；若涉及危化试剂配套，需人工确认资质。",
                        "意图标签：实验设备选型,预算匹配,资质边界",
                    ]
                ),
            ),
        ]
    if normalized == "used_car":
        return [
            (
                group_name,
                "\n".join(
                    [
                        f"商品资料：记录员正常产品 {batch_token}",
                        f"测试批次：{batch_token}",
                        "商品名称：2020款别克GL8 ES陆尊653T豪华型",
                        "型号：CHEJIN-LIVE-GL8-2020ES",
                        "商品类目：二手车/MPV",
                        "价格：17.66万",
                        "单位：台",
                        "库存：1",
                        "发货：南京门店可看车，商务客户试乘需人工确认",
                        "售后：车况以检测报告为准，事故、水泡、火烧承诺必须人工确认",
                    ]
                ),
            ),
            (
                group_name,
                "\n".join(
                    [
                        f"政策规则：记录员缺字段边界 {batch_token}",
                        f"测试批次：{batch_token}",
                        "规则名称：新能源电池检测转人工",
                        "规则类型：contract",
                        f"触发关键词：电池检测,首付,月供,贷款包过,{batch_token}",
                    ]
                ),
            ),
            (
                group_name,
                f"边界噪音：{batch_token}_NOISE 今天天气和测试心跳记录，不是产品资料，也不是客服知识。",
            ),
            (
                file_target,
                "\n".join(
                    [
                        f"聊天记录：记录员文件传输助手话术 {batch_token}",
                        f"客户：我想买新能源通勤，能看秦PLUS吗？批次 {batch_token}",
                        "客服：可以先看秦PLUS DM-i，低油耗适合通勤；电池检测、当地迁入和金融方案都需要人工确认。",
                        "意图标签：新能源通勤,车源推荐,金融边界",
                    ]
                ),
            ),
        ]
    return [
        (
            group_name,
            "\n".join(
                [
                    f"商品资料：记录员正常产品 {batch_token}",
                    f"测试批次：{batch_token}",
                    "商品名称：变频空调 1.5P 一级能效",
                    "型号：HOME-LIVE-AC15P",
                    "商品类目：家电/空调",
                    "价格：3299",
                    "单位：台",
                    "库存：12",
                    "发货：华东仓72小时内发货",
                    "售后：整机3年质保，压缩机10年质保",
                ]
            ),
        ),
        (
            group_name,
            "\n".join(
                [
                    f"政策规则：记录员缺字段边界 {batch_token}",
                    f"测试批次：{batch_token}",
                    "规则名称：跨区安装资质转人工",
                    "规则类型：contract",
                    f"触发关键词：高空作业,跨区安装,商用电改造,{batch_token}",
                ]
            ),
        ),
        (
            group_name,
            f"边界噪音：{batch_token}_NOISE 今天天气和测试心跳记录，不是产品资料，也不是客服知识。",
        ),
        (
            file_target,
            "\n".join(
                [
                    f"聊天记录：记录员文件传输助手话术 {batch_token}",
                    f"客户：出租屋想装一台省电空调，预算3000左右。批次 {batch_token}",
                    "客服：可先看1.5P一级能效机型，省电且适合日常通勤居住；跨区安装和电路改造需人工确认。",
                    "意图标签：预算匹配,节能诉求,安装边界",
                ]
            ),
        ),
    ]


def historical_replay_messages(
    *,
    tenant_id: str,
    group_name: str,
    batch_token: str,
    sample_size: int,
    explicit_artifact: str,
) -> dict[str, Any] | None:
    artifact_path = resolve_historical_artifact_path(tenant_id=tenant_id, explicit_artifact=explicit_artifact)
    if artifact_path is None:
        return None
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return None
    unique: list[str] = []
    seen: set[str] = set()
    for item in messages:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if content.startswith("@"):
            continue
        normalized = " ".join(content.replace("\r", "\n").split())
        if len(normalized) < 8:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(content)
    if not unique:
        return None
    desired = max(1, min(int(sample_size or 4), 12, len(unique)))
    rng = random.Random(int(hashlib.md5(batch_token.encode("utf-8")).hexdigest(), 16) & 0xFFFFFFFF)
    selected = unique[:]
    rng.shuffle(selected)
    selected = selected[:desired]
    entries: list[dict[str, Any]] = []
    for idx, content in enumerate(selected, start=1):
        token = f"{batch_token}_H{idx:02d}"
        compact = compact_historical_message(content, max_chars=180)
        text = "\n".join([f"历史样本批次：{token}", compact, f"样本标记：{token}"])
        entries.append({"target": group_name, "text": text, "token": token, "required": True})
    return {"source_file": str(artifact_path), "entries": entries}


def compact_historical_message(text: str, *, max_chars: int) -> str:
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    head = cleaned[: max(1, max_chars - 8)].rstrip()
    return head + "\n(截断)"


def resolve_historical_artifact_path(*, tenant_id: str, explicit_artifact: str) -> Path | None:
    explicit = str(explicit_artifact or "").strip()
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    artifact_root = tenant_runtime_root(tenant_id) / "test_artifacts" / "live_send"
    if not artifact_root.exists():
        return None
    files = sorted(artifact_root.glob("live_sim_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def infer_industry_from_historical_artifact(*, tenant_id: str, history_artifact: str) -> str:
    artifact_path = resolve_historical_artifact_path(tenant_id=tenant_id, explicit_artifact=history_artifact)
    if artifact_path is None:
        return ""
    payload: dict[str, Any] = {}
    try:
        parsed = json.loads(artifact_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            payload = parsed
    except (OSError, json.JSONDecodeError):
        payload = {}
    name = artifact_path.name.lower()
    full = str(artifact_path).lower()
    source_file = str(payload.get("source_file") or "").lower()
    message_text = ""
    messages = payload.get("messages")
    if isinstance(messages, list):
        fragments: list[str] = []
        for item in messages[:20]:
            if isinstance(item, dict):
                fragments.append(str(item.get("content") or ""))
        message_text = " ".join(fragments).lower()
    text = f"{name} {full} {source_file} {message_text}"
    if any(keyword in text for keyword in ("实验", "lab", "仪器", "细胞", "试剂")):
        return "lab_instruments"
    if any(keyword in text for keyword in ("chejin", "二手车", "used_car", "car")):
        return "used_car"
    return ""


def expected_tokens(batch_token: str) -> list[str]:
    return [
        f"商品资料：记录员正常产品 {batch_token}",
        f"政策规则：记录员缺字段边界 {batch_token}",
        f"{batch_token}_NOISE",
        f"记录员文件传输助手话术 {batch_token}",
    ]


def token_from_text(text: str) -> str:
    for line in text.splitlines():
        if "LIVE_RECORDER_" in line:
            return line.strip()
    return text[:80]


def enforce_selected_targets(service: RecorderService, target_names: list[str]) -> None:
    selected = {str(name or "").strip() for name in target_names if str(name or "").strip()}
    for conversation in service.list_conversations(status="all"):
        conversation_id = str(conversation.get("conversation_id") or "").strip()
        target_name = str(conversation.get("target_name") or "").strip()
        if not conversation_id or not target_name:
            continue
        expected = target_name in selected
        if bool(conversation.get("selected_by_user")) == expected:
            continue
        service.update_conversation(conversation_id, {"selected_by_user": expected})


def baseline_conversation(connector: WeChatConnector, raw_store: RawMessageStore, conversation: dict[str, Any]) -> dict[str, Any]:
    target = str(conversation.get("target_name") or "")
    payload = connector.get_messages(target, exact=conversation.get("exact", True) is not False)
    if not payload.get("ok"):
        return {"ok": False, "target": target, "messages": payload}
    result = raw_store.upsert_messages(
        conversation,
        [item for item in payload.get("messages", []) or [] if isinstance(item, dict)],
        source_module="smart_recorder_live_baseline",
        learning_enabled=False,
        create_batch=False,
        batch_reason="live_test_baseline",
    )
    return {"ok": True, "target": target, "baseline_message_count": len(payload.get("messages", []) or []), "result": result}


def retry_capture_until_tokens(
    service: RecorderService,
    raw_store: RawMessageStore,
    tokens: list[str],
    *,
    attempts: int,
    settle_seconds: float,
) -> dict[str, Any]:
    captures = []
    for _ in range(max(1, attempts)):
        capture = service.capture_selected_once(send_notifications=False)
        captures.append(capture)
        raw_checks = verify_raw_tokens(raw_store, tokens)
        if all(item.get("found") for item in raw_checks):
            return {"ok": True, "captures": captures, "raw_checks": raw_checks}
        time.sleep(max(0.5, settle_seconds))
    return {"ok": False, "captures": captures, "raw_checks": verify_raw_tokens(raw_store, tokens)}


def verify_raw_tokens(raw_store: RawMessageStore, tokens: list[str]) -> list[dict[str, Any]]:
    messages = raw_store.list_messages(limit=500)

    def normalize_probe(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).lower()

    def token_present(content: str, token: str) -> bool:
        raw_content = str(content or "")
        raw_token = str(token or "")
        if not raw_token:
            return False
        if raw_token in raw_content:
            return True
        normalized_token = normalize_probe(raw_token)
        if not normalized_token:
            return False
        return normalized_token in normalize_probe(raw_content)

    return [
        {
            "token": token,
            "found": any(token_present(str(item.get("content") or ""), token) for item in messages),
            "message_ids": [item.get("raw_message_id") for item in messages if token_present(str(item.get("content") or ""), token)],
        }
        for token in tokens
    ]


def verify_token_idempotency(service: RecorderService, raw_store: RawMessageStore, tokens: list[str]) -> dict[str, Any]:
    before = raw_token_message_ids(raw_store, tokens)
    capture = service.capture_selected_once(send_notifications=False)
    after = raw_token_message_ids(raw_store, tokens)
    checks = [
        {
            "token": token,
            "before_ids": before.get(token, []),
            "after_ids": after.get(token, []),
            "unchanged": before.get(token, []) == after.get(token, []),
        }
        for token in tokens
    ]
    return {
        "ok": all(item["unchanged"] for item in checks),
        "capture": capture,
        "token_checks": checks,
    }


def raw_token_message_ids(raw_store: RawMessageStore, tokens: list[str]) -> dict[str, list[str]]:
    messages = raw_store.list_messages(limit=500)
    def normalize_probe(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).lower()
    def token_present(content: str, token: str) -> bool:
        raw_content = str(content or "")
        raw_token = str(token or "")
        if not raw_token:
            return False
        if raw_token in raw_content:
            return True
        normalized_token = normalize_probe(raw_token)
        if not normalized_token:
            return False
        return normalized_token in normalize_probe(raw_content)
    return {
        token: sorted(
            str(item.get("raw_message_id") or "")
            for item in messages
            if token_present(str(item.get("content") or ""), token)
        )
        for token in tokens
    }


def verify_rag_tokens(tokens: list[str], *, required_tokens: set[str] | None = None) -> list[dict[str, Any]]:
    rag = RagService()
    required_set = set(required_tokens or []) if required_tokens is not None else None
    results = []
    for token in tokens:
        search = rag.search(token, limit=30)
        found = any(token in str(hit.get("text") or "") for hit in search.get("hits", []) or [])
        required = ("_NOISE" not in token) if required_set is None else (token in required_set)
        results.append({"token": token, "found": found, "required": required, "search": compact_search(search)})
    return results


def compact_search(search: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": search.get("ok"),
        "hit_count": len(search.get("hits", []) or []),
        "hits": [
            {"source_id": hit.get("source_id"), "score": hit.get("score"), "text": str(hit.get("text") or "")[:180]}
            for hit in (search.get("hits", []) or [])[:3]
        ],
    }


def verify_candidate_expectations(batch_token: str) -> dict[str, Any]:
    candidates = list_candidate_payloads()

    def contains(token: str, candidate: dict[str, Any]) -> bool:
        return token in candidate_structured_text(candidate)

    normal = [item for item in candidates if contains(f"商品资料：记录员正常产品 {batch_token}", item)]
    file_transfer = [item for item in candidates if contains(f"记录员文件传输助手话术 {batch_token}", item)]
    incomplete = [item for item in candidates if contains(f"政策规则：记录员缺字段边界 {batch_token}", item)]
    noise = [item for item in candidates if contains(f"{batch_token}_NOISE", item)]
    return {
        "normal_candidate_found": bool(normal),
        "normal_candidate_ids": [item.get("candidate_id") for item in normal],
        "file_transfer_candidate_found": bool(file_transfer),
        "file_transfer_candidate_ids": [item.get("candidate_id") for item in file_transfer],
        "incomplete_candidate_found": bool(incomplete),
        "incomplete_candidate_ids": [item.get("candidate_id") for item in incomplete],
        "incomplete_statuses": [candidate_status(item) for item in incomplete],
        "noise_candidate_found": bool(noise),
        "noise_candidate_ids": [item.get("candidate_id") for item in noise],
    }


def candidate_structured_text(candidate: dict[str, Any]) -> str:
    proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
    patch = proposal.get("formal_patch") if isinstance(proposal.get("formal_patch"), dict) else {}
    item = patch.get("item") if isinstance(patch.get("item"), dict) else {}
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    payload = {
        "candidate_id": candidate.get("candidate_id"),
        "target_category": patch.get("target_category"),
        "summary": proposal.get("summary"),
        "suggested_fields": proposal.get("suggested_fields"),
        "item_data": item.get("data") if isinstance(item.get("data"), dict) else {},
        "evidence_excerpt": source.get("evidence_excerpt"),
    }
    return json.dumps(payload, ensure_ascii=False)


def list_candidate_payloads() -> list[dict[str, Any]]:
    items = []
    for status in ("pending", "approved", "rejected"):
        root = tenant_review_candidates_root() / status
        if not root.exists():
            continue
        for path in root.glob("*.json"):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return items


def candidate_status(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "review_status": (candidate.get("review") or {}).get("status"),
        "completeness_status": (candidate.get("review") or {}).get("completeness_status") or (candidate.get("intake") or {}).get("status"),
        "missing_fields": (candidate.get("intake") or {}).get("missing_fields") or (candidate.get("review") or {}).get("missing_fields") or [],
    }


def preferred_module_for_industry(industry_id: str) -> str:
    normalized = str(industry_id or "").strip().lower()
    if normalized == "lab_instruments":
        return "order_sheet_lab_v1"
    return "raw_message_log_v1"


def ensure_live_test_module_binding(*, tenant_id: str, industry_id: str) -> dict[str, Any]:
    registry = RecorderModuleRegistryService()
    module_key = preferred_module_for_industry(industry_id)
    modules = registry.list_modules(include_inactive=True)
    if not any(str(item.get("module_key") or "") == module_key for item in modules):
        fallback = modules[0].get("module_key") if modules else ""
        if not fallback:
            raise RuntimeError("no active recorder modules available")
        module_key = str(fallback)
    payload = {
        "binding_id": f"live_test_tenant_{tenant_id}",
        "scope_type": "tenant",
        "scope_id": tenant_id,
        "tenant_id": tenant_id,
        "module_key": module_key,
        "enabled": True,
    }
    item = registry.upsert_binding(payload)
    return {"preferred_module_key": module_key, "binding": item}


if __name__ == "__main__":
    raise SystemExit(main())
