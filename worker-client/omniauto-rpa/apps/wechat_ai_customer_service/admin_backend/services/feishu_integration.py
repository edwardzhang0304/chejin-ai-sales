"""Feishu notification integration for human handoff events."""

from __future__ import annotations

import base64
import hmac
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id


PROJECT_ROOT = Path(__file__).resolve().parents[4]
FEISHU_CONFIG_PATH = PROJECT_ROOT / "runtime" / "apps" / "wechat_ai_customer_service" / "admin" / "feishu_config.json"
FEISHU_OPEN_API_BASE = "https://open.feishu.cn/open-apis"
MASKED_SECRET_PREFIX = "已配置"
FEISHU_MODES = {"webhook", "app_bot"}
RECEIVE_ID_TYPES = {"open_id", "user_id", "union_id", "email", "chat_id"}
FEISHU_PLAIN_CONFIG_KEYS = (
    "enabled",
    "mode",
    "app_id",
    "receive_id_type",
    "default_receive_ids",
    "bound_accounts",
    "notify_on_handoff",
    "notify_on_logout",
    "timeout_seconds",
)
FEISHU_SECRET_CONFIG_KEYS = ("webhook_url", "webhook_secret", "app_secret")


UrlopenFn = Callable[..., Any]


def default_feishu_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "mode": "webhook",
        "webhook_url": "",
        "webhook_secret": "",
        "app_id": "",
        "app_secret": "",
        "receive_id_type": "open_id",
        "default_receive_ids": [],
        "bound_accounts": [],
        "notify_on_handoff": True,
        "notify_on_logout": True,
        "timeout_seconds": 8,
    }


def load_feishu_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or FEISHU_CONFIG_PATH
    if not config_path.exists():
        return default_feishu_config()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return normalize_feishu_config(raw)


def save_feishu_config(payload: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    config_path = path or FEISHU_CONFIG_PATH
    current = load_feishu_config(config_path)
    normalized = merge_feishu_config_payload(payload, base=current)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def merge_feishu_config_payload(payload: dict[str, Any], *, base: dict[str, Any] | None = None) -> dict[str, Any]:
    current = normalize_feishu_config(base or {})
    merged = dict(current)
    for key in FEISHU_PLAIN_CONFIG_KEYS:
        if key in payload:
            merged[key] = payload.get(key)
    for secret_key in FEISHU_SECRET_CONFIG_KEYS:
        clear_key = f"clear_{secret_key}"
        incoming = str(payload.get(secret_key) or "").strip()
        if payload.get(clear_key):
            merged[secret_key] = ""
        elif incoming and not is_masked_secret(incoming):
            merged[secret_key] = incoming
    return normalize_feishu_config(merged)


def normalize_feishu_config(payload: dict[str, Any]) -> dict[str, Any]:
    config = default_feishu_config()
    config.update({key: value for key, value in payload.items() if key in config})
    mode = str(config.get("mode") or "webhook").strip().lower()
    config["mode"] = mode if mode in FEISHU_MODES else "webhook"
    receive_id_type = str(config.get("receive_id_type") or "open_id").strip().lower()
    config["receive_id_type"] = receive_id_type if receive_id_type in RECEIVE_ID_TYPES else "open_id"
    config["enabled"] = bool(config.get("enabled"))
    config["notify_on_handoff"] = bool(config.get("notify_on_handoff", True))
    config["notify_on_logout"] = bool(config.get("notify_on_logout", True))
    config["webhook_url"] = str(config.get("webhook_url") or "").strip()
    config["webhook_secret"] = str(config.get("webhook_secret") or "").strip()
    config["app_id"] = str(config.get("app_id") or "").strip()
    config["app_secret"] = str(config.get("app_secret") or "").strip()
    config["default_receive_ids"] = normalize_receive_ids(config.get("default_receive_ids"))
    config["bound_accounts"] = normalize_bound_accounts(config.get("bound_accounts"), fallback_receive_id_type=config["receive_id_type"])
    try:
        timeout = float(config.get("timeout_seconds") or 8)
    except (TypeError, ValueError):
        timeout = 8
    config["timeout_seconds"] = max(2, min(timeout, 30))
    return config


def normalize_receive_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [line.strip() for line in value.replace(",", "\n").splitlines()]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        items = []
    return [item for item in dict.fromkeys(items) if item]


def normalize_bound_accounts(value: Any, *, fallback_receive_id_type: str = "open_id") -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else []
    normalized: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        receive_id = str(raw.get("receive_id") or raw.get("id") or "").strip()
        if not receive_id:
            continue
        receive_id_type = str(raw.get("receive_id_type") or fallback_receive_id_type or "open_id").strip().lower()
        if receive_id_type not in RECEIVE_ID_TYPES:
            receive_id_type = fallback_receive_id_type if fallback_receive_id_type in RECEIVE_ID_TYPES else "open_id"
        normalized.append(
            {
                "label": str(raw.get("label") or raw.get("name") or receive_id).strip(),
                "tenant_id": str(raw.get("tenant_id") or "").strip(),
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "enabled": raw.get("enabled") is not False,
            }
        )
    return normalized


def public_feishu_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    source = normalize_feishu_config(config or load_feishu_config())
    return {
        "ok": True,
        "enabled": source["enabled"],
        "mode": source["mode"],
        "webhook_url_configured": bool(source["webhook_url"]),
        "webhook_url_masked": mask_secret(source["webhook_url"]),
        "webhook_secret_configured": bool(source["webhook_secret"]),
        "webhook_secret_masked": mask_secret(source["webhook_secret"]),
        "app_id": source["app_id"],
        "app_secret_configured": bool(source["app_secret"]),
        "app_secret_masked": mask_secret(source["app_secret"]),
        "receive_id_type": source["receive_id_type"],
        "receive_id_types": sorted(RECEIVE_ID_TYPES),
        "default_receive_ids": source["default_receive_ids"],
        "bound_accounts": source["bound_accounts"],
        "notify_on_handoff": source["notify_on_handoff"],
        "notify_on_logout": source["notify_on_logout"],
        "timeout_seconds": source["timeout_seconds"],
        "editable": True,
    }


def mask_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 10:
        return "****"
    return text[:4] + "****" + text[-4:]


def is_masked_secret(value: str) -> bool:
    text = str(value or "").strip()
    return not text or "****" in text or text.startswith(MASKED_SECRET_PREFIX)


def feishu_webhook_sign(timestamp: int | str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def validate_feishu_config(config: dict[str, Any], *, tenant_id: str = "") -> list[str]:
    errors: list[str] = []
    if not config.get("enabled"):
        return errors
    mode = config.get("mode")
    if mode == "webhook":
        if not config.get("webhook_url"):
            errors.append("webhook_url_required")
    elif mode == "app_bot":
        if not config.get("app_id"):
            errors.append("app_id_required")
        if not config.get("app_secret"):
            errors.append("app_secret_required")
        has_bound_targets = any(item.get("enabled", True) for item in config.get("bound_accounts", []) or [])
        if not resolve_feishu_targets(config, tenant_id=tenant_id) and not has_bound_targets:
            errors.append("receive_ids_required")
    else:
        errors.append("unsupported_mode")
    return errors


def test_feishu_connection(
    *,
    config: dict[str, Any] | None = None,
    path: Path | None = None,
    dry_run: bool = False,
    urlopen_fn: UrlopenFn | None = None,
) -> dict[str, Any]:
    source = normalize_feishu_config(config or load_feishu_config(path))
    errors = validate_feishu_config(source)
    if errors:
        return {"ok": False, "enabled": source.get("enabled"), "status": "invalid_config", "errors": errors}
    if not source.get("enabled"):
        return {"ok": True, "enabled": False, "status": "disabled", "message": "飞书通知未启用。"}
    text = "OmniAuto 转人工通知测试：如果你看到这条消息，说明飞书连接已可用。"
    return send_feishu_text(source, text=text, dry_run=dry_run, urlopen_fn=urlopen_fn)


def dispatch_handoff_case_to_feishu(
    case: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    path: Path | None = None,
    dry_run: bool = False,
    urlopen_fn: UrlopenFn | None = None,
) -> dict[str, Any]:
    source = normalize_feishu_config(config or load_feishu_config(path))
    if not source.get("enabled") or not source.get("notify_on_handoff", True):
        return {"enabled": False, "status": "not_configured", "adapter": "feishu", "reason": "feishu_handoff_notify_disabled"}
    if is_logout_handoff_case(case) and not source.get("notify_on_logout", True):
        return {"enabled": False, "status": "skipped", "adapter": "feishu", "reason": "logout_notify_disabled"}
    errors = validate_feishu_config(source, tenant_id=str(case.get("tenant_id") or ""))
    if errors:
        return {"enabled": True, "ok": False, "status": "invalid_config", "adapter": "feishu", "errors": errors}
    text = format_handoff_message(case)
    result = send_feishu_text(source, text=text, tenant_id=str(case.get("tenant_id") or ""), dry_run=dry_run, urlopen_fn=urlopen_fn)
    result["adapter"] = "feishu"
    result["case_id"] = case.get("case_id")
    return result


def is_logout_handoff_case(case: dict[str, Any]) -> bool:
    reason = str(case.get("reason") or "")
    return "logout" in reason or "login" in reason


def format_handoff_message(case: dict[str, Any]) -> str:
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    reason = str(case.get("reason") or inner.get("reason") or "需要人工接管")
    target = str(case.get("target") or "微信运行态")
    tenant_id = str(case.get("tenant_id") or active_tenant_id())
    case_id = str(case.get("case_id") or "")
    messages = [str(item).strip() for item in case.get("message_contents", []) or [] if str(item).strip()]
    preview = messages[0] if messages else str(inner.get("message") or "")
    if len(preview) > 300:
        preview = preview[:300] + "..."
    return "\n".join(
        [
            "【OmniAuto 转人工通知】",
            f"租户：{tenant_id}",
            f"会话：{target}",
            f"原因：{reason}",
            f"工单：{case_id or '未生成'}",
            f"内容：{preview or '无'}",
            "处理建议：请人工查看微信窗口和后台工单，确认后再恢复自动监听。",
        ]
    )


def send_feishu_text(
    config: dict[str, Any],
    *,
    text: str,
    tenant_id: str = "",
    dry_run: bool = False,
    urlopen_fn: UrlopenFn | None = None,
) -> dict[str, Any]:
    source = normalize_feishu_config(config)
    mode = source.get("mode")
    if mode == "webhook":
        return send_feishu_webhook_text(source, text=text, dry_run=dry_run, urlopen_fn=urlopen_fn)
    if mode == "app_bot":
        return send_feishu_app_bot_text(source, text=text, tenant_id=tenant_id, dry_run=dry_run, urlopen_fn=urlopen_fn)
    return {"ok": False, "enabled": source.get("enabled"), "status": "unsupported_mode", "mode": mode}


def send_feishu_webhook_text(
    config: dict[str, Any],
    *,
    text: str,
    dry_run: bool = False,
    urlopen_fn: UrlopenFn | None = None,
) -> dict[str, Any]:
    webhook_url = str(config.get("webhook_url") or "").strip()
    payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
    if config.get("webhook_secret"):
        timestamp = int(time.time())
        payload["timestamp"] = str(timestamp)
        payload["sign"] = feishu_webhook_sign(timestamp, str(config.get("webhook_secret") or ""))
    if dry_run:
        return {"ok": True, "enabled": True, "status": "dry_run", "mode": "webhook", "target_count": 1, "payload": payload}
    response = post_json(webhook_url, payload=payload, timeout=float(config.get("timeout_seconds") or 8), urlopen_fn=urlopen_fn)
    ok = response.get("ok") and int(response.get("code") or 0) == 0
    return {**response, "ok": bool(ok), "enabled": True, "status": "sent" if ok else "failed", "mode": "webhook", "target_count": 1}


def send_feishu_app_bot_text(
    config: dict[str, Any],
    *,
    text: str,
    tenant_id: str = "",
    dry_run: bool = False,
    urlopen_fn: UrlopenFn | None = None,
) -> dict[str, Any]:
    targets = resolve_feishu_targets(config, tenant_id=tenant_id)
    if not targets:
        return {"ok": False, "enabled": True, "status": "no_targets", "mode": "app_bot", "target_count": 0}
    if dry_run:
        return {"ok": True, "enabled": True, "status": "dry_run", "mode": "app_bot", "target_count": len(targets), "targets": safe_targets(targets)}
    token_result = fetch_tenant_access_token(config, urlopen_fn=urlopen_fn)
    if not token_result.get("ok"):
        return {**token_result, "enabled": True, "status": "token_failed", "mode": "app_bot", "target_count": len(targets)}
    token = str(token_result.get("tenant_access_token") or "")
    results = []
    for target in targets:
        payload = {
            "receive_id": target["receive_id"],
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        url = f"{FEISHU_OPEN_API_BASE}/im/v1/messages?receive_id_type={target['receive_id_type']}"
        results.append(
            post_json(
                url,
                payload=payload,
                timeout=float(config.get("timeout_seconds") or 8),
                headers={"Authorization": f"Bearer {token}"},
                urlopen_fn=urlopen_fn,
            )
        )
    ok = all(item.get("ok") and int(item.get("code") or 0) == 0 for item in results)
    return {
        "ok": ok,
        "enabled": True,
        "status": "sent" if ok else "partial_failed",
        "mode": "app_bot",
        "target_count": len(targets),
        "targets": safe_targets(targets),
        "results": summarize_post_results(results),
    }


def resolve_feishu_targets(config: dict[str, Any], *, tenant_id: str = "") -> list[dict[str, str]]:
    tenant = str(tenant_id or "").strip()
    targets: list[dict[str, str]] = []
    for item in config.get("bound_accounts", []) or []:
        if not item.get("enabled", True):
            continue
        item_tenant = str(item.get("tenant_id") or "").strip()
        if item_tenant and tenant and item_tenant != tenant:
            continue
        targets.append(
            {
                "label": str(item.get("label") or item.get("receive_id") or ""),
                "receive_id": str(item.get("receive_id") or ""),
                "receive_id_type": str(item.get("receive_id_type") or config.get("receive_id_type") or "open_id"),
            }
        )
    if not targets:
        for receive_id in config.get("default_receive_ids", []) or []:
            targets.append(
                {
                    "label": receive_id,
                    "receive_id": receive_id,
                    "receive_id_type": str(config.get("receive_id_type") or "open_id"),
                }
            )
    return [item for item in targets if item.get("receive_id")]


def safe_targets(targets: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "label": item.get("label", ""),
            "receive_id_type": item.get("receive_id_type", ""),
            "receive_id_masked": mask_secret(item.get("receive_id", "")),
        }
        for item in targets
    ]


def fetch_tenant_access_token(config: dict[str, Any], *, urlopen_fn: UrlopenFn | None = None) -> dict[str, Any]:
    payload = {"app_id": config.get("app_id"), "app_secret": config.get("app_secret")}
    result = post_json(
        f"{FEISHU_OPEN_API_BASE}/auth/v3/tenant_access_token/internal",
        payload=payload,
        timeout=float(config.get("timeout_seconds") or 8),
        urlopen_fn=urlopen_fn,
    )
    if result.get("ok") and int(result.get("code") or 0) == 0 and result.get("tenant_access_token"):
        return {"ok": True, "tenant_access_token": result.get("tenant_access_token"), "expire": result.get("expire")}
    return {**result, "ok": False}


def post_json(
    url: str,
    *,
    payload: dict[str, Any],
    timeout: float,
    headers: dict[str, str] | None = None,
    urlopen_fn: UrlopenFn | None = None,
) -> dict[str, Any]:
    opener = urlopen_fn or urllib.request.urlopen
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with opener(request, timeout=max(2, timeout)) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return {"ok": False, "http_status": exc.code, "error": detail}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc.reason)}
    except Exception as exc:  # noqa: BLE001 - notification failures must stay non-fatal.
        return {"ok": False, "error": repr(exc)}
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        data = {"raw": raw}
    if isinstance(data, dict):
        return {"ok": 200 <= status < 300, "http_status": status, **data}
    return {"ok": 200 <= status < 300, "http_status": status, "raw": data}


def summarize_post_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "ok": bool(item.get("ok")),
            "http_status": item.get("http_status"),
            "code": item.get("code"),
            "msg": item.get("msg") or item.get("message") or item.get("error"),
        }
        for item in results
    ]
