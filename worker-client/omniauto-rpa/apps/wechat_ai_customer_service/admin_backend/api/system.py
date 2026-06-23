"""System status APIs."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, Request

from apps.wechat_ai_customer_service.auth.models import Role
from apps.wechat_ai_customer_service.llm_config import (
    LLM_REASONING_EFFORT_OPTIONS,
    active_llm_provider,
    call_llm_request_once,
    llm_provider_options,
    llm_provider_preset,
    llm_provider_request_style,
    llm_route_display_label,
    llm_urlopen,
    load_llm_config,
    llm_fallback_enabled,
    normalize_llm_base_url,
    normalize_llm_provider,
    normalize_llm_reasoning_effort,
    parse_bool,
    resolve_llm_fallback_settings,
    resolve_llm_api_key,
    resolve_llm_allow_insecure_tls,
    resolve_llm_base_url,
    resolve_llm_reasoning_effort,
    resolve_llm_tier_model,
    save_llm_config,
)
from apps.wechat_ai_customer_service.workflows.llm_output_adapter import llm_adapter_profile
from apps.wechat_ai_customer_service.platform_safety_rules import load_platform_safety_rules, save_platform_safety_rules
from apps.wechat_ai_customer_service.platform_understanding_rules import load_platform_understanding_rules, save_platform_understanding_rules
from ..auth_context import current_auth_context
from ..services.diagnostics_service import DiagnosticsService
from ..services.feishu_integration import (
    dispatch_handoff_case_to_feishu,
    load_feishu_config,
    merge_feishu_config_payload,
    public_feishu_config,
    save_feishu_config,
    test_feishu_connection,
)
from ..services.handoff_store import HandoffStore
from ..services.knowledge_store import KnowledgeStore
from ..services.locks import list_runtime_locks
from ..services.runtime_monitor import RuntimeMonitor
from ..services.version_store import VersionStore
from ..services.work_queue import WorkQueueService


router = APIRouter(prefix="/api/system", tags=["system"])
knowledge = KnowledgeStore()
diagnostics = DiagnosticsService()
versions = VersionStore()
work_queue = WorkQueueService()
handoffs = HandoffStore()
monitor = RuntimeMonitor()


@router.get("/status")
def status() -> dict[str, Any]:
    overview = knowledge.overview()
    runs = diagnostics.list_runs()
    return {
        "ok": True,
        "knowledge": overview,
        "recent_diagnostic": runs[0] if runs else None,
        "versions": {"count": len(versions.list_versions())},
        "work_queue": work_queue.summary(),
        "handoffs": handoffs.summary(),
        "readiness": monitor.readiness(),
        "locks": list_runtime_locks(),
    }


@router.get("/runtime-locks")
def runtime_locks() -> dict[str, Any]:
    return {"ok": True, "items": list_runtime_locks()}


@router.get("/readiness")
def readiness() -> dict[str, Any]:
    report = monitor.readiness()
    return {"ok": report["ok"], "report": report}


@router.get("/platform-safety-rules")
def platform_safety_rules(request: Request) -> dict[str, Any]:
    context = current_auth_context(request)
    payload = load_platform_safety_rules()
    item = payload.get("item", {})
    editable = context.role == Role.ADMIN and payload.get("readonly") is not True
    return {
        "ok": bool(payload.get("ok")),
        "path": payload.get("path"),
        "source": payload.get("source", "local_file"),
        "error": payload.get("error", ""),
        "readonly": payload.get("readonly") is True,
        "editable": editable,
        "item": item,
    }


@router.put("/platform-safety-rules")
def update_platform_safety_rules(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    context = current_auth_context(request)
    if context.role != Role.ADMIN:
        return {"ok": False, "detail": "only admin can update platform safety rules"}
    item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
    result = save_platform_safety_rules(item)
    return {
        "ok": bool(result.get("ok")),
        "path": result.get("path"),
        "source": result.get("source", "local_file"),
        "error": result.get("error", ""),
        "readonly": result.get("readonly") is True,
        "item": result.get("item"),
    }


@router.get("/platform-understanding-rules")
def platform_understanding_rules(request: Request) -> dict[str, Any]:
    context = current_auth_context(request)
    payload = load_platform_understanding_rules()
    item = payload.get("item", {})
    editable = context.role == Role.ADMIN and payload.get("readonly") is not True
    return {
        "ok": bool(payload.get("ok")),
        "path": payload.get("path"),
        "source": payload.get("source", "local_file"),
        "error": payload.get("error", ""),
        "readonly": payload.get("readonly") is True,
        "editable": editable,
        "item": item,
    }


@router.put("/platform-understanding-rules")
def update_platform_understanding_rules(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    context = current_auth_context(request)
    if context.role != Role.ADMIN:
        return {"ok": False, "detail": "only admin can update platform understanding rules"}
    item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
    result = save_platform_understanding_rules(item)
    return {
        "ok": bool(result.get("ok")),
        "path": result.get("path"),
        "source": result.get("source", "local_file"),
        "error": result.get("error", ""),
        "readonly": result.get("readonly") is True,
        "item": result.get("item"),
    }


@router.post("/heartbeat/{component_id}")
def heartbeat(component_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    return {
        "ok": True,
        "item": monitor.heartbeat(
            component_id,
            status=str(payload.get("status") or "ok"),
            message=str(payload.get("message") or ""),
            payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
        ),
    }


@router.get("/llm-config")
def llm_config(request: Request) -> dict[str, Any]:
    context = current_auth_context(request)
    config = load_llm_config()
    provider = active_llm_provider(config=config)
    return _llm_config_payload(context=context, config=config, provider=provider)


@router.put("/llm-config")
def update_llm_config(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    context = current_auth_context(request)
    config = load_llm_config()
    legacy_key_only = set(payload.keys()) <= {"deepseek_api_key"}
    provider_value = payload.get("provider")
    if not provider_value and "deepseek_api_key" in payload and "api_key" not in payload:
        provider_value = "deepseek"
    provider = normalize_llm_provider(provider_value or config.get("LLM_PROVIDER") or "deepseek")
    preset = llm_provider_preset(provider)
    config["LLM_PROVIDER"] = provider

    base_url = normalize_llm_base_url(str(payload.get("base_url") or ""))
    flash_model = str(payload.get("flash_model") or payload.get("model") or "").strip()
    pro_model = str(payload.get("pro_model") or payload.get("model") or "").strip()
    flash_reasoning_effort = normalize_llm_reasoning_effort(payload.get("flash_reasoning_effort"))
    pro_reasoning_effort = normalize_llm_reasoning_effort(payload.get("pro_reasoning_effort"))
    api_key = str(payload.get("api_key") or payload.get("deepseek_api_key") or "").strip()
    clear_api_key = bool(payload.get("clear_api_key")) or (legacy_key_only and not api_key)
    allow_insecure_tls = parse_bool(payload.get("allow_insecure_tls"), default=False)

    if "base_url" in payload:
        _set_or_clear(config, str(preset.get("base_url_env") or ""), base_url)
    if "flash_model" in payload or "model" in payload:
        _set_or_clear(config, str(preset.get("flash_model_env") or ""), flash_model)
    if "pro_model" in payload or "model" in payload:
        _set_or_clear(config, str(preset.get("pro_model_env") or ""), pro_model)
    if "flash_reasoning_effort" in payload:
        _set_or_clear(config, str(preset.get("flash_reasoning_effort_env") or ""), flash_reasoning_effort)
    if "pro_reasoning_effort" in payload:
        _set_or_clear(config, str(preset.get("pro_reasoning_effort_env") or ""), pro_reasoning_effort)
    if "allow_insecure_tls" in payload:
        _set_bool(config, str(preset.get("allow_insecure_tls_env") or ""), allow_insecure_tls)
    if api_key:
        config[str(preset.get("api_key_env") or "")] = api_key
    elif clear_api_key:
        config.pop(str(preset.get("api_key_env") or ""), None)
        if provider == "openai_compatible":
            config.pop("LLM_API_KEY", None)

    fallback_enabled = parse_bool(payload.get("fallback_enabled"), default=llm_fallback_enabled(config=config))
    fallback_provider_value = payload.get("fallback_provider")
    fallback_provider = normalize_llm_provider(
        fallback_provider_value or config.get("LLM_FALLBACK_PROVIDER") or "anthropic"
    ) if (fallback_enabled or "fallback_provider" in payload) else resolve_llm_fallback_settings(config=config).get("provider", "")
    fallback_base_url = normalize_llm_base_url(str(payload.get("fallback_base_url") or ""))
    fallback_flash_model = str(payload.get("fallback_flash_model") or payload.get("fallback_model") or "").strip()
    fallback_pro_model = str(payload.get("fallback_pro_model") or payload.get("fallback_model") or "").strip()
    fallback_flash_reasoning_effort = normalize_llm_reasoning_effort(payload.get("fallback_flash_reasoning_effort"))
    fallback_pro_reasoning_effort = normalize_llm_reasoning_effort(payload.get("fallback_pro_reasoning_effort"))
    fallback_api_key = str(payload.get("fallback_api_key") or "").strip()
    fallback_clear_api_key = bool(payload.get("fallback_clear_api_key"))
    fallback_allow_insecure_tls = parse_bool(payload.get("fallback_allow_insecure_tls"), default=False)

    if "fallback_enabled" in payload:
        _set_bool(config, "LLM_FALLBACK_ENABLED", fallback_enabled)
    if "fallback_provider" in payload:
        _set_or_clear(config, "LLM_FALLBACK_PROVIDER", fallback_provider)
    if "fallback_base_url" in payload:
        _set_or_clear(config, "LLM_FALLBACK_BASE_URL", fallback_base_url)
    if "fallback_flash_model" in payload or "fallback_model" in payload:
        _set_or_clear(config, "LLM_FALLBACK_FLASH_MODEL", fallback_flash_model)
    if "fallback_pro_model" in payload or "fallback_model" in payload:
        _set_or_clear(config, "LLM_FALLBACK_PRO_MODEL", fallback_pro_model)
    if "fallback_flash_reasoning_effort" in payload:
        _set_or_clear(config, "LLM_FALLBACK_FLASH_REASONING_EFFORT", fallback_flash_reasoning_effort)
    if "fallback_pro_reasoning_effort" in payload:
        _set_or_clear(config, "LLM_FALLBACK_PRO_REASONING_EFFORT", fallback_pro_reasoning_effort)
    if "fallback_allow_insecure_tls" in payload:
        _set_bool(config, "LLM_FALLBACK_ALLOW_INSECURE_TLS", fallback_allow_insecure_tls)
    if fallback_api_key:
        config["LLM_FALLBACK_API_KEY"] = fallback_api_key
    elif fallback_clear_api_key:
        config.pop("LLM_FALLBACK_API_KEY", None)
    save_llm_config(config)
    return _llm_config_payload(context=context, config=config, provider=provider)


@router.post("/llm-config/test")
def test_llm_config(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    context = current_auth_context(request)
    payload = payload or {}
    config = load_llm_config()
    target = "fallback" if str(payload.get("target") or "").strip().lower() == "fallback" else "primary"
    tier = "pro" if str(payload.get("route") or payload.get("tier") or "").strip().lower() == "pro" else "flash"
    if target == "fallback":
        fallback = resolve_llm_fallback_settings(config=config, tier=tier)
        provider = normalize_llm_provider(payload.get("provider") or fallback.get("provider") or "")
        api_key = str(payload.get("api_key") or "").strip() or str(fallback.get("api_key") or "")
        base_url = normalize_llm_base_url(str(payload.get("base_url") or "")) or str(fallback.get("base_url") or "")
        requested_model = payload.get("model")
        if requested_model is None:
            requested_model = payload.get("pro_model") if tier == "pro" else payload.get("flash_model")
        model = str(requested_model or "").strip() or str(fallback.get("model") or "")
        reasoning_effort = normalize_llm_reasoning_effort(
            payload.get("reasoning_effort")
            if "reasoning_effort" in payload
            else (fallback.get("pro_reasoning_effort") if tier == "pro" else fallback.get("flash_reasoning_effort"))
        )
        allow_insecure_tls = (
            parse_bool(payload.get("allow_insecure_tls"), default=bool(fallback.get("allow_insecure_tls")))
            if "allow_insecure_tls" in payload
            else bool(fallback.get("allow_insecure_tls"))
        )
    else:
        provider = normalize_llm_provider(payload.get("provider") or active_llm_provider(config=config))
        api_key = str(payload.get("api_key") or "").strip() or resolve_llm_api_key(provider=provider, config=config)
        base_url = normalize_llm_base_url(str(payload.get("base_url") or "")) or resolve_llm_base_url(provider=provider, config=config)
        requested_model = payload.get("model")
        if requested_model is None:
            requested_model = payload.get("pro_model") if tier == "pro" else payload.get("flash_model")
        model = str(requested_model or "").strip() or resolve_llm_tier_model(provider=provider, tier=tier, config=config)
        reasoning_effort = normalize_llm_reasoning_effort(payload.get("reasoning_effort")) or resolve_llm_reasoning_effort(
            provider=provider,
            tier=tier,
            config=config,
        )
        allow_insecure_tls = payload.get("allow_insecure_tls")
    if not provider:
        return {"ok": False, "message": "供应商未配置", "target": target}
    if not api_key:
        return {"ok": False, "message": f"{llm_provider_preset(provider).get('label', provider)} API Key 未配置", "provider": provider, "target": target}
    if not base_url:
        return {"ok": False, "message": "Base URL 未配置", "provider": provider, "target": target}
    if not model:
        return {"ok": False, "message": "模型名未配置", "provider": provider, "base_url": base_url, "target": target}
    response = call_llm_request_once(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        messages=[{"role": "user", "content": "Reply with OK only."}],
        timeout=30,
        max_tokens=8,
        temperature=0,
        tier=tier,
        json_mode=False,
        explicit_reasoning_effort=reasoning_effort,
        allow_insecure_tls=allow_insecure_tls,
    )
    if response.get("ok"):
        return {
            "ok": True,
            "message": "连接成功",
            "target": target,
            "provider": provider,
            "provider_label": str(llm_provider_preset(provider).get("label") or provider),
            "route_display_label": llm_route_display_label(
                provider=provider,
                model=model,
                request_style=response.get("request_style", llm_provider_request_style(provider)),
            ),
            "base_url": base_url,
            "model": model,
            "route": tier,
            "reasoning_effort": reasoning_effort,
            "sample": str(response.get("response_text") or "")[:80],
            "request_style": response.get("request_style", llm_provider_request_style(provider)),
        }
    model_hint = _llm_model_unavailable_hint(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        allow_insecure_tls=allow_insecure_tls,
    )
    message = str(response.get("error") or f"HTTP {response.get('status') or 0}")
    if model_hint:
        message = f"{message}\n{model_hint}"
    return {
        "ok": False,
        "message": message,
        "target": target,
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "request_style": response.get("request_style", llm_provider_request_style(provider)),
    }


@router.get("/feishu-config")
def feishu_config(request: Request) -> dict[str, Any]:
    current_auth_context(request)
    return public_feishu_config(load_feishu_config())


@router.put("/feishu-config")
def update_feishu_config(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    context = current_auth_context(request)
    if context.role != Role.ADMIN:
        return {"ok": False, "detail": "only admin can update Feishu handoff settings"}
    config = save_feishu_config(payload)
    return public_feishu_config(config)


@router.post("/feishu-config/test")
def test_feishu_config(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    context = current_auth_context(request)
    if context.role != Role.ADMIN:
        return {"ok": False, "detail": "only admin can test Feishu handoff settings"}
    payload = payload or {}
    config = load_feishu_config()
    feishu_payload_keys = (
        "enabled",
        "mode",
        "webhook_url",
        "webhook_secret",
        "app_id",
        "app_secret",
        "receive_id_type",
        "default_receive_ids",
        "bound_accounts",
    )
    if any(key in payload for key in feishu_payload_keys):
        config = merge_feishu_config_payload(payload, base=config)
    dry_run = bool(payload.get("dry_run"))
    return test_feishu_connection(config=config, dry_run=dry_run)


@router.post("/feishu-config/test-handoff")
def test_feishu_handoff(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    context = current_auth_context(request)
    if context.role != Role.ADMIN:
        return {"ok": False, "detail": "only admin can test Feishu handoff settings"}
    payload = payload or {}
    case = {
        "tenant_id": context.tenant_id,
        "case_id": "handoff_test_preview",
        "target": str(payload.get("target") or "文件传输助手"),
        "reason": str(payload.get("reason") or "manual_handoff_test"),
        "message_contents": [str(payload.get("message") or "这是一条转人工通知测试。")],
        "payload": {"payload": {"kind": "manual_handoff_test"}},
    }
    config = load_feishu_config()
    feishu_payload_keys = (
        "enabled",
        "mode",
        "webhook_url",
        "webhook_secret",
        "app_id",
        "app_secret",
        "receive_id_type",
        "default_receive_ids",
        "bound_accounts",
    )
    if any(key in payload for key in feishu_payload_keys):
        config = merge_feishu_config_payload(payload, base=config)
    return dispatch_handoff_case_to_feishu(case, config=config, dry_run=bool(payload.get("dry_run")))


def _route_probe_payload(
    *,
    provider: str,
    provider_label: str,
    base_url: str,
    flash_model: str,
    pro_model: str,
    flash_reasoning_effort: str,
    pro_reasoning_effort: str,
    api_key: str,
    allow_insecure_tls: bool,
    enabled: bool | None = None,
) -> dict[str, Any]:
    model_probe = _fetch_provider_model_ids(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        allow_insecure_tls=allow_insecure_tls,
        timeout=4,
    )
    available_models = model_probe.get("models") if isinstance(model_probe.get("models"), list) else []
    request_style = llm_provider_request_style(provider)
    route_display_label = llm_route_display_label(provider=provider, model=flash_model or pro_model, request_style=request_style)
    adapter = llm_adapter_profile(provider=provider, model=flash_model or pro_model, request_style=request_style)
    return {
        "enabled": enabled if enabled is not None else True,
        "provider": provider,
        "provider_label": provider_label,
        "route_display_label": route_display_label,
        "base_url": base_url,
        "flash_model": flash_model,
        "pro_model": pro_model,
        "flash_reasoning_effort": flash_reasoning_effort,
        "pro_reasoning_effort": pro_reasoning_effort,
        "available_models": available_models,
        "available_models_error": str(model_probe.get("error") or ""),
        "allow_insecure_tls": allow_insecure_tls,
        "api_key_configured": bool(api_key),
        "api_key_masked": _mask_key(api_key),
        "request_style": request_style,
        "adapter_profile": adapter.get("id"),
        "adapter_label": adapter.get("label"),
        "adapter_notes": adapter.get("notes", []),
    }


def _llm_config_payload(*, context: Any, config: dict[str, str], provider: str) -> dict[str, Any]:
    provider = normalize_llm_provider(provider)
    key = resolve_llm_api_key(provider=provider, config=config)
    providers = []
    for option in llm_provider_options(config=config):
        provider_key = resolve_llm_api_key(provider=option.get("id"), config=config)
        providers.append({**option, "api_key_masked": _mask_key(provider_key)})
    preset = llm_provider_preset(provider)
    base_url = resolve_llm_base_url(provider=provider, config=config)
    primary_route = _route_probe_payload(
        provider=provider,
        provider_label=str(preset.get("label") or provider),
        base_url=base_url,
        flash_model=resolve_llm_tier_model(provider=provider, tier="flash", config=config),
        pro_model=resolve_llm_tier_model(provider=provider, tier="pro", config=config),
        flash_reasoning_effort=resolve_llm_reasoning_effort(provider=provider, tier="flash", config=config),
        pro_reasoning_effort=resolve_llm_reasoning_effort(provider=provider, tier="pro", config=config),
        api_key=key,
        allow_insecure_tls=resolve_llm_allow_insecure_tls(provider=provider, config=config),
    )
    fallback_settings = resolve_llm_fallback_settings(config=config, tier="flash")
    fallback_provider = normalize_llm_provider(fallback_settings.get("provider") or "")
    fallback_label = str(llm_provider_preset(fallback_provider).get("label") or fallback_provider or "")
    fallback_route = _route_probe_payload(
        provider=fallback_provider,
        provider_label=fallback_label,
        base_url=str(fallback_settings.get("base_url") or ""),
        flash_model=str(fallback_settings.get("flash_model") or ""),
        pro_model=str(fallback_settings.get("pro_model") or ""),
        flash_reasoning_effort=str(fallback_settings.get("flash_reasoning_effort") or ""),
        pro_reasoning_effort=str(fallback_settings.get("pro_reasoning_effort") or ""),
        api_key=str(fallback_settings.get("api_key") or ""),
        allow_insecure_tls=bool(fallback_settings.get("allow_insecure_tls")),
        enabled=bool(fallback_settings.get("enabled")),
    ) if fallback_provider else {
        "enabled": bool(fallback_settings.get("enabled")),
        "provider": "",
        "provider_label": "",
        "base_url": "",
        "flash_model": "",
        "pro_model": "",
        "flash_reasoning_effort": "",
        "pro_reasoning_effort": "",
        "available_models": [],
        "available_models_error": "",
        "allow_insecure_tls": False,
        "api_key_configured": False,
        "api_key_masked": "",
        "request_style": "",
        "adapter_profile": "",
        "adapter_label": "",
        "adapter_notes": [],
    }
    return {
        "ok": True,
        "provider": provider,
        "provider_label": str(preset.get("label") or provider),
        "providers": providers,
        "base_url": primary_route["base_url"],
        "flash_model": primary_route["flash_model"],
        "pro_model": primary_route["pro_model"],
        "flash_reasoning_effort": primary_route["flash_reasoning_effort"],
        "pro_reasoning_effort": primary_route["pro_reasoning_effort"],
        "reasoning_effort_options": list(LLM_REASONING_EFFORT_OPTIONS),
        "available_models": primary_route["available_models"],
        "available_models_error": primary_route["available_models_error"],
        "allow_insecure_tls": primary_route["allow_insecure_tls"],
        "api_key_configured": primary_route["api_key_configured"],
        "api_key_masked": primary_route["api_key_masked"],
        "request_style": primary_route["request_style"],
        "adapter_profile": primary_route["adapter_profile"],
        "adapter_label": primary_route["adapter_label"],
        "adapter_notes": primary_route["adapter_notes"],
        "fallback": fallback_route,
        "editable": True,
    }


def _set_or_clear(config: dict[str, str], key: str, value: str) -> None:
    if not key:
        return
    if value:
        config[key] = value
    else:
        config.pop(key, None)


def _set_bool(config: dict[str, str], key: str, value: bool) -> None:
    if not key:
        return
    if value:
        config[key] = "1"
    else:
        config.pop(key, None)


def _chat_completion_sample(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:80]
    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    return str(content or "").strip()[:80]


def _llm_model_unavailable_hint(*, provider: str, base_url: str, api_key: str, model: str, allow_insecure_tls: Any = None) -> str:
    probe = _fetch_provider_model_ids(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        allow_insecure_tls=allow_insecure_tls,
        timeout=8,
    )
    models = probe.get("models") if isinstance(probe.get("models"), list) else []
    if models and model not in models:
        shown = ", ".join(str(item) for item in models[:12])
        more = f" 等 {len(models)} 个" if len(models) > 12 else ""
        return f"当前中转 /models 未列出 `{model}`，可用模型只有：{shown}{more}。请在中转站开通/映射该模型，或改用列表中的模型。"
    error = str(probe.get("error") or "")
    if error:
        return f"无法读取当前中转 /models 列表：{error}"
    return ""


def _fetch_provider_model_ids(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    allow_insecure_tls: Any = None,
    timeout: int = 6,
) -> dict[str, Any]:
    if not base_url or not api_key:
        return {"ok": False, "models": [], "error": ""}
    try:
        request_style = llm_provider_request_style(provider)
        headers = {"Accept": "application/json"}
        if request_style == "anthropic_messages":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(
            base_url.rstrip("/") + "/models",
            headers=headers,
            method="GET",
        )
        with llm_urlopen(req, timeout=max(1, timeout), provider=provider, allow_insecure_tls=allow_insecure_tls) as response:
            raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:160]
        return {"ok": False, "models": [], "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "models": [], "error": str(exc)}
    items = data.get("data") if isinstance(data, dict) else []
    models = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                model_id = str(item.get("id") or "").strip()
                if model_id and model_id not in models:
                    models.append(model_id)
    return {"ok": True, "models": models, "error": ""}


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]
