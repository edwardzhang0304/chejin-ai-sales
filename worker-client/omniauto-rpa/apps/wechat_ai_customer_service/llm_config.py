"""Shared LLM provider configuration for the WeChat customer-service app."""

from __future__ import annotations

import json
import os
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_PRO_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_FLASH_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_MODEL = DEFAULT_DEEPSEEK_FLASH_MODEL
DEFAULT_DEEPSEEK_CONTEXT_WINDOW_TOKENS = 1_000_000
DEFAULT_DEEPSEEK_TIMEOUT_SECONDS = 120
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_FLASH_MODEL = "gpt-5.4"
DEFAULT_OPENAI_PRO_MODEL = "gpt-5.4"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_LLM_PROVIDER = "deepseek"
LLM_REASONING_EFFORT_OPTIONS = ("", "none", "minimal", "low", "medium", "high", "xhigh")
TRANSIENT_LLM_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
TRANSIENT_LLM_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "temporary unavailable",
    "connection reset",
    "connection aborted",
    "remote end closed connection",
    "remotedisconnected",
    "connection refused",
    "connection closed",
    "gateway timeout",
    "service unavailable",
    "too many requests",
    "rate limit",
    "read operation timed out",
    "name or service not known",
    "nodename nor servname provided",
    "temporary failure in name resolution",
    "network is unreachable",
    "eof occurred in violation of protocol",
)
MODEL_UNAVAILABLE_LLM_ERROR_MARKERS = (
    "model is not supported",
    "model not supported",
    "unsupported model",
    "invalid model",
    "model does not exist",
    "model not found",
    "model_not_found",
    "does not have access to model",
    "model is unavailable",
    "model unavailable",
)
GATEWAY_FAILOVERABLE_LLM_ERROR_MARKERS = (
    "upstream request failed",
    "upstream_error",
)


LLM_PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "model_env": "DEEPSEEK_MODEL",
        "flash_model_env": "DEEPSEEK_FLASH_MODEL",
        "pro_model_env": "DEEPSEEK_PRO_MODEL",
        "flash_reasoning_effort_env": "DEEPSEEK_FLASH_REASONING_EFFORT",
        "pro_reasoning_effort_env": "DEEPSEEK_PRO_REASONING_EFFORT",
        "allow_insecure_tls_env": "DEEPSEEK_ALLOW_INSECURE_TLS",
        "default_base_url": DEFAULT_DEEPSEEK_BASE_URL,
        "default_flash_model": DEFAULT_DEEPSEEK_FLASH_MODEL,
        "default_pro_model": DEFAULT_DEEPSEEK_PRO_MODEL,
        "model_options": [
            DEFAULT_DEEPSEEK_FLASH_MODEL,
            DEFAULT_DEEPSEEK_PRO_MODEL,
            "deepseek-chat",
            "deepseek-reasoner",
        ],
        "aliases": ("deepseek", "deepseek-chat"),
    },
    "openai": {
        "label": "OpenAI / ChatGPT",
        "api_key_env": "OPENAI_API_KEY",
        "base_url_env": "OPENAI_BASE_URL",
        "model_env": "OPENAI_MODEL",
        "flash_model_env": "OPENAI_FLASH_MODEL",
        "pro_model_env": "OPENAI_PRO_MODEL",
        "flash_reasoning_effort_env": "OPENAI_FLASH_REASONING_EFFORT",
        "pro_reasoning_effort_env": "OPENAI_PRO_REASONING_EFFORT",
        "allow_insecure_tls_env": "OPENAI_ALLOW_INSECURE_TLS",
        "default_base_url": DEFAULT_OPENAI_BASE_URL,
        "default_flash_model": DEFAULT_OPENAI_FLASH_MODEL,
        "default_pro_model": DEFAULT_OPENAI_PRO_MODEL,
        "model_options": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"],
        "aliases": ("openai", "gpt", "chatgpt"),
    },
    "openai_compatible": {
        "label": "OpenAI Compatible / Custom",
        "api_key_env": "OPENAI_COMPATIBLE_API_KEY",
        "base_url_env": "OPENAI_COMPATIBLE_BASE_URL",
        "model_env": "OPENAI_COMPATIBLE_MODEL",
        "flash_model_env": "OPENAI_COMPATIBLE_FLASH_MODEL",
        "pro_model_env": "OPENAI_COMPATIBLE_PRO_MODEL",
        "flash_reasoning_effort_env": "OPENAI_COMPATIBLE_FLASH_REASONING_EFFORT",
        "pro_reasoning_effort_env": "OPENAI_COMPATIBLE_PRO_REASONING_EFFORT",
        "allow_insecure_tls_env": "OPENAI_COMPATIBLE_ALLOW_INSECURE_TLS",
        "default_base_url": "",
        "default_flash_model": "",
        "default_pro_model": "",
        "model_options": [],
        "aliases": ("openai-compatible", "openai_compatible", "compatible", "custom", "third_party", "third-party"),
    },
    "anthropic": {
        "label": "Anthropic Compatible / Claude & Kimi",
        "api_key_env": "ANTHROPIC_AUTH_TOKEN",
        "api_key_env_aliases": ["ANTHROPIC_API_KEY"],
        "base_url_env": "ANTHROPIC_BASE_URL",
        "model_env": "ANTHROPIC_MODEL",
        "flash_model_env": "ANTHROPIC_FLASH_MODEL",
        "pro_model_env": "ANTHROPIC_PRO_MODEL",
        "flash_reasoning_effort_env": "ANTHROPIC_FLASH_REASONING_EFFORT",
        "pro_reasoning_effort_env": "ANTHROPIC_PRO_REASONING_EFFORT",
        "allow_insecure_tls_env": "ANTHROPIC_ALLOW_INSECURE_TLS",
        "default_base_url": DEFAULT_ANTHROPIC_BASE_URL,
        "default_flash_model": "",
        "default_pro_model": "",
        "model_options": ["kimi-for-coding"],
        "aliases": ("anthropic", "claude", "anthropic-compatible", "anthropic_compatible"),
        "request_style": "anthropic_messages",
    },
    "qwen": {
        "label": "Alibaba Qwen",
        "api_key_env": "QWEN_API_KEY",
        "base_url_env": "QWEN_BASE_URL",
        "model_env": "QWEN_MODEL",
        "flash_model_env": "QWEN_FLASH_MODEL",
        "pro_model_env": "QWEN_PRO_MODEL",
        "flash_reasoning_effort_env": "QWEN_FLASH_REASONING_EFFORT",
        "pro_reasoning_effort_env": "QWEN_PRO_REASONING_EFFORT",
        "allow_insecure_tls_env": "QWEN_ALLOW_INSECURE_TLS",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_flash_model": "qwen-plus",
        "default_pro_model": "qwen-max",
        "model_options": ["qwen-plus", "qwen-max", "qwen-turbo"],
        "aliases": ("qwen", "dashscope", "aliyun", "alibaba"),
    },
    "moonshot": {
        "label": "Moonshot / Kimi",
        "api_key_env": "MOONSHOT_API_KEY",
        "base_url_env": "MOONSHOT_BASE_URL",
        "model_env": "MOONSHOT_MODEL",
        "flash_model_env": "MOONSHOT_FLASH_MODEL",
        "pro_model_env": "MOONSHOT_PRO_MODEL",
        "flash_reasoning_effort_env": "MOONSHOT_FLASH_REASONING_EFFORT",
        "pro_reasoning_effort_env": "MOONSHOT_PRO_REASONING_EFFORT",
        "allow_insecure_tls_env": "MOONSHOT_ALLOW_INSECURE_TLS",
        "default_base_url": "https://api.moonshot.cn/v1",
        "default_flash_model": "moonshot-v1-8k",
        "default_pro_model": "moonshot-v1-32k",
        "model_options": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "aliases": ("moonshot", "kimi"),
    },
    "zhipu": {
        "label": "Zhipu GLM",
        "api_key_env": "ZHIPU_API_KEY",
        "base_url_env": "ZHIPU_BASE_URL",
        "model_env": "ZHIPU_MODEL",
        "flash_model_env": "ZHIPU_FLASH_MODEL",
        "pro_model_env": "ZHIPU_PRO_MODEL",
        "flash_reasoning_effort_env": "ZHIPU_FLASH_REASONING_EFFORT",
        "pro_reasoning_effort_env": "ZHIPU_PRO_REASONING_EFFORT",
        "allow_insecure_tls_env": "ZHIPU_ALLOW_INSECURE_TLS",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_flash_model": "glm-4-flash",
        "default_pro_model": "glm-4-plus",
        "model_options": ["glm-4-flash", "glm-4-plus", "glm-4"],
        "aliases": ("zhipu", "bigmodel", "glm"),
    },
    "siliconflow": {
        "label": "SiliconFlow",
        "api_key_env": "SILICONFLOW_API_KEY",
        "base_url_env": "SILICONFLOW_BASE_URL",
        "model_env": "SILICONFLOW_MODEL",
        "flash_model_env": "SILICONFLOW_FLASH_MODEL",
        "pro_model_env": "SILICONFLOW_PRO_MODEL",
        "flash_reasoning_effort_env": "SILICONFLOW_FLASH_REASONING_EFFORT",
        "pro_reasoning_effort_env": "SILICONFLOW_PRO_REASONING_EFFORT",
        "allow_insecure_tls_env": "SILICONFLOW_ALLOW_INSECURE_TLS",
        "default_base_url": "https://api.siliconflow.cn/v1",
        "default_flash_model": "",
        "default_pro_model": "",
        "model_options": [],
        "aliases": ("siliconflow", "silicon_flow", "silicon-flow"),
    },
}

_PROVIDER_ALIASES = {
    alias: provider_id
    for provider_id, preset in LLM_PROVIDER_PRESETS.items()
    for alias in (provider_id, *preset.get("aliases", ()))
}


_LLM_CONFIG_PATH: Path | None = None


def llm_config_path() -> Path:
    global _LLM_CONFIG_PATH
    if _LLM_CONFIG_PATH is None:
        root = Path(__file__).resolve().parent
        _LLM_CONFIG_PATH = root.parents[1] / "runtime" / "apps" / "wechat_ai_customer_service" / "llm_config.json"
    return _LLM_CONFIG_PATH


def load_llm_config() -> dict[str, str]:
    path = llm_config_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return {str(k): str(v) for k, v in payload.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_llm_config(config: dict[str, str]) -> None:
    path = llm_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


SecretReader = Callable[[str], str]


def read_secret(name: str) -> str:
    """Read a secret from config file first, then process env, then Windows registry."""
    config = load_llm_config()
    if name == "DEEPSEEK_API_KEY":
        provider = active_llm_provider(config=config)
        if provider != "deepseek":
            return resolve_llm_api_key(provider=provider, config=config)
    if name == "DEEPSEEK_BASE_URL":
        provider = active_llm_provider(config=config)
        if provider != "deepseek":
            return resolve_llm_base_url(provider=provider, config=config)
    if name in {"DEEPSEEK_MODEL", "DEEPSEEK_FLASH_MODEL"}:
        provider = active_llm_provider(config=config)
        if provider != "deepseek":
            return resolve_llm_tier_model(provider=provider, tier="flash", config=config)
    if name == "DEEPSEEK_PRO_MODEL":
        provider = active_llm_provider(config=config)
        if provider != "deepseek":
            return resolve_llm_tier_model(provider=provider, tier="pro", config=config)
    if name == "DEEPSEEK_FLASH_REASONING_EFFORT":
        provider = active_llm_provider(config=config)
        if provider != "deepseek":
            return resolve_llm_reasoning_effort(provider=provider, tier="flash", config=config)
    if name == "DEEPSEEK_PRO_REASONING_EFFORT":
        provider = active_llm_provider(config=config)
        if provider != "deepseek":
            return resolve_llm_reasoning_effort(provider=provider, tier="pro", config=config)
    return _read_named_value(name, config=config)


def normalize_llm_provider(provider: Any) -> str:
    text = str(provider or "").strip().lower().replace("-", "_")
    if not text:
        return DEFAULT_LLM_PROVIDER
    return _PROVIDER_ALIASES.get(text, text if text in LLM_PROVIDER_PRESETS else "openai_compatible")


def active_llm_provider(*, config: dict[str, str] | None = None) -> str:
    return configured_llm_provider(config=config) or DEFAULT_LLM_PROVIDER


def configured_llm_provider(*, config: dict[str, str] | None = None) -> str:
    config = config if config is not None else load_llm_config()
    value = (
        config.get("LLM_PROVIDER")
        or config.get("ACTIVE_LLM_PROVIDER")
        or os.getenv("LLM_PROVIDER")
        or os.getenv("ACTIVE_LLM_PROVIDER")
        or _read_registry_value("LLM_PROVIDER")
        or _read_registry_value("ACTIVE_LLM_PROVIDER")
    )
    return normalize_llm_provider(value) if value else ""


def resolve_effective_llm_provider(
    explicit_provider: Any = None,
    *,
    read_secret_fn: SecretReader | None = read_secret,
) -> str:
    explicit = str(explicit_provider or "").strip()
    if explicit.lower() == "manual_json":
        return "manual_json"
    if read_secret_fn is read_secret:
        configured = configured_llm_provider()
        if configured:
            return configured
    if explicit:
        return normalize_llm_provider(explicit)
    return DEFAULT_LLM_PROVIDER


def active_provider_overrides_explicit(
    explicit_provider: Any = None,
    effective_provider: Any | None = None,
    *,
    config: dict[str, str] | None = None,
    read_secret_fn: SecretReader | None = read_secret,
) -> bool:
    """Return True when the global active provider intentionally overrides a module default.

    Tenant configs often keep provider-scoped defaults such as
    ``provider=deepseek`` and ``model=deepseek-v4-flash``. When the operator
    switches the active provider to OpenAI, those provider-scoped values should
    not keep leaking into individual modules.
    """
    explicit = str(explicit_provider or "").strip()
    if not explicit or explicit.lower() == "manual_json":
        return False
    if read_secret_fn is not read_secret:
        return False
    configured = configured_llm_provider(config=config)
    if not configured:
        return False
    explicit_id = normalize_llm_provider(explicit)
    effective_id = normalize_llm_provider(effective_provider or configured)
    return bool(effective_id == configured and explicit_id != configured)


def llm_provider_preset(provider: Any) -> dict[str, Any]:
    provider_id = normalize_llm_provider(provider)
    return LLM_PROVIDER_PRESETS.get(provider_id, LLM_PROVIDER_PRESETS[DEFAULT_LLM_PROVIDER])


def llm_route_display_label(*, provider: Any, model: Any = "", request_style: Any = "") -> str:
    """Return an operator-facing route label that reflects gateway/model reality."""

    provider_id = normalize_llm_provider(provider)
    preset_label = str(llm_provider_preset(provider_id).get("label") or provider_id)
    model_text = str(model or "").strip()
    style_text = str(request_style or llm_provider_request_style(provider_id) or "").strip()
    lower = f"{provider_id} {model_text} {style_text}".lower()
    if "kimi" in lower or "moonshot" in lower:
        return "Kimi (Anthropic-compatible)" if style_text == "anthropic_messages" else "Kimi"
    if provider_id == "anthropic":
        return "Kimi / Anthropic-compatible"
    if provider_id == "deepseek" or model_text.lower().startswith("deepseek"):
        return "DeepSeek"
    return preset_label


def explicit_model_matches_provider(provider: Any, model: Any) -> bool:
    """Guard against stale provider-scoped model names after provider switches."""
    provider_id = normalize_llm_provider(provider)
    if provider_id == "openai_compatible":
        return True
    detected = detect_provider_from_model_name(model)
    if provider_id == "anthropic":
        # Anthropic-compatible routes may serve Claude or Kimi/Moonshot-style
        # models, but should not inherit stale OpenAI/DeepSeek/Qwen/etc. model
        # names after the operator switches the global route.
        return not detected or detected == "moonshot"
    return not detected or detected == provider_id


def detect_provider_from_model_name(model: Any) -> str:
    text = str(model or "").strip().lower()
    if not text:
        return ""
    provider_prefixes = {
        "deepseek": ("deepseek",),
        "openai": ("gpt-", "o1", "o3", "o4", "o5"),
        "qwen": ("qwen",),
        "moonshot": ("moonshot", "kimi"),
        "zhipu": ("glm", "charglm"),
    }
    for provider_id, prefixes in provider_prefixes.items():
        if any(text.startswith(prefix) for prefix in prefixes):
            return provider_id
    return ""


def explicit_base_url_matches_provider(provider: Any, base_url: Any) -> bool:
    """Allow custom gateways, but ignore URLs that clearly belong to another provider."""
    provider_id = normalize_llm_provider(provider)
    if provider_id in {"openai_compatible", "anthropic"}:
        return True
    detected = detect_provider_from_base_url(base_url)
    return not detected or detected == provider_id


def detect_provider_from_base_url(base_url: Any) -> str:
    text = str(base_url or "").strip().lower()
    if not text:
        return ""
    provider_domains = {
        "deepseek": ("deepseek.com",),
        "openai": ("openai.com",),
        "qwen": ("dashscope.aliyuncs.com", "aliyuncs.com"),
        "moonshot": ("moonshot.cn",),
        "zhipu": ("bigmodel.cn",),
        "siliconflow": ("siliconflow.cn",),
    }
    for provider_id, domains in provider_domains.items():
        if any(domain in text for domain in domains):
            return provider_id
    return ""


def llm_provider_options(*, config: dict[str, str] | None = None) -> list[dict[str, Any]]:
    config = config if config is not None else load_llm_config()
    options = []
    for provider_id, preset in LLM_PROVIDER_PRESETS.items():
        options.append(
            {
                "id": provider_id,
                "label": str(preset.get("label") or provider_id),
                "base_url": resolve_llm_base_url(provider=provider_id, config=config),
                "flash_model": resolve_llm_tier_model(provider=provider_id, tier="flash", config=config),
                "pro_model": resolve_llm_tier_model(provider=provider_id, tier="pro", config=config),
                "flash_reasoning_effort": resolve_llm_reasoning_effort(provider=provider_id, tier="flash", config=config),
                "pro_reasoning_effort": resolve_llm_reasoning_effort(provider=provider_id, tier="pro", config=config),
                "model_options": list(preset.get("model_options") or []),
                "api_key_configured": bool(resolve_llm_api_key(provider=provider_id, config=config)),
                "allow_insecure_tls": resolve_llm_allow_insecure_tls(provider=provider_id, config=config),
            }
        )
    return options


def resolve_llm_api_key(
    *,
    provider: Any | None = None,
    config: dict[str, str] | None = None,
    read_secret_fn: SecretReader | None = None,
) -> str:
    provider_id = normalize_llm_provider(provider or active_llm_provider(config=config))
    preset = llm_provider_preset(provider_id)
    names = [str(preset.get("api_key_env") or ""), *[str(item or "") for item in (preset.get("api_key_env_aliases") or [])]]
    if provider_id == "openai_compatible":
        names.append("LLM_API_KEY")
    return _first_value(names, config=config, read_secret_fn=read_secret_fn)


def resolve_llm_base_url(
    *,
    provider: Any | None = None,
    explicit_base_url: str | None = None,
    config: dict[str, str] | None = None,
    read_secret_fn: SecretReader | None = None,
) -> str:
    explicit = normalize_llm_base_url(explicit_base_url)
    provider_id = normalize_llm_provider(provider or active_llm_provider(config=config))
    if explicit and explicit_base_url_matches_provider(provider_id, explicit):
        return explicit
    preset = llm_provider_preset(provider_id)
    names = [str(preset.get("base_url_env") or "")]
    if provider_id == "openai_compatible":
        names.append("LLM_BASE_URL")
    configured = normalize_llm_base_url(_first_value(names, config=config, read_secret_fn=read_secret_fn))
    return configured or str(preset.get("default_base_url") or "").strip()


def resolve_llm_model(
    *,
    provider: Any | None = None,
    explicit_model: str | None = None,
    config: dict[str, str] | None = None,
    read_secret_fn: SecretReader | None = None,
) -> str:
    explicit = str(explicit_model or "").strip()
    provider_id = normalize_llm_provider(provider or active_llm_provider(config=config))
    if explicit and explicit_model_matches_provider(provider_id, explicit):
        return explicit
    preset = llm_provider_preset(provider_id)
    names = [str(preset.get("model_env") or ""), str(preset.get("flash_model_env") or "")]
    if provider_id == "openai_compatible":
        names.extend(["LLM_MODEL", "LLM_FLASH_MODEL"])
    configured = _first_value(names, config=config, read_secret_fn=read_secret_fn).strip()
    return configured or str(preset.get("default_flash_model") or "").strip()


def resolve_llm_tier_model(
    *,
    provider: Any | None = None,
    tier: str,
    explicit_model: str | None = None,
    config: dict[str, str] | None = None,
    read_secret_fn: SecretReader | None = None,
) -> str:
    explicit = str(explicit_model or "").strip()
    provider_id = normalize_llm_provider(provider or active_llm_provider(config=config))
    if explicit and explicit_model_matches_provider(provider_id, explicit):
        return explicit
    preset = llm_provider_preset(provider_id)
    normalized = normalize_deepseek_model_tier(tier)
    if normalized == "pro":
        names = [str(preset.get("pro_model_env") or ""), str(preset.get("model_env") or "")]
        if provider_id == "openai_compatible":
            names.extend(["LLM_PRO_MODEL", "LLM_MODEL"])
        configured = _first_value(names, config=config, read_secret_fn=read_secret_fn).strip()
        return configured or str(preset.get("default_pro_model") or preset.get("default_flash_model") or "").strip()
    names = [str(preset.get("flash_model_env") or ""), str(preset.get("model_env") or "")]
    if provider_id == "openai_compatible":
        names.extend(["LLM_FLASH_MODEL", "LLM_MODEL"])
    configured = _first_value(names, config=config, read_secret_fn=read_secret_fn).strip()
    return configured or str(preset.get("default_flash_model") or "").strip()


def normalize_llm_reasoning_effort(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "auto": "",
        "default": "",
        "inherit": "",
        "off": "",
        "disabled": "",
        "disable": "",
        "x-high": "xhigh",
        "extra-high": "xhigh",
        "extra": "xhigh",
    }
    text = aliases.get(text, text).replace("-", "")
    return text if text in LLM_REASONING_EFFORT_OPTIONS else ""


def resolve_llm_reasoning_effort(
    *,
    provider: Any | None = None,
    tier: str,
    explicit_value: Any | None = None,
    config: dict[str, str] | None = None,
    read_secret_fn: SecretReader | None = None,
) -> str:
    if explicit_value is not None:
        return normalize_llm_reasoning_effort(explicit_value)
    provider_id = normalize_llm_provider(provider or active_llm_provider(config=config))
    preset = llm_provider_preset(provider_id)
    normalized = normalize_deepseek_model_tier(tier)
    if normalized == "pro":
        names = [str(preset.get("pro_reasoning_effort_env") or "")]
        if provider_id == "openai_compatible":
            names.append("LLM_PRO_REASONING_EFFORT")
    else:
        names = [str(preset.get("flash_reasoning_effort_env") or "")]
        if provider_id == "openai_compatible":
            names.append("LLM_FLASH_REASONING_EFFORT")
    return normalize_llm_reasoning_effort(_first_value(names, config=config, read_secret_fn=read_secret_fn))


def apply_llm_reasoning_effort(
    payload: dict[str, Any],
    *,
    provider: Any | None = None,
    tier: str = "flash",
    explicit_value: Any | None = None,
    config: dict[str, str] | None = None,
    read_secret_fn: SecretReader | None = read_secret,
) -> str:
    effort = resolve_llm_reasoning_effort(
        provider=provider or _legacy_provider_for_reader(read_secret_fn) if read_secret_fn else provider,
        tier=tier,
        explicit_value=explicit_value,
        config=config,
        read_secret_fn=read_secret_fn,
    )
    if effort:
        payload["reasoning_effort"] = effort
    return effort


def resolve_llm_allow_insecure_tls(
    *,
    provider: Any | None = None,
    explicit_value: Any | None = None,
    config: dict[str, str] | None = None,
    read_secret_fn: SecretReader | None = None,
) -> bool:
    if explicit_value is not None:
        return parse_bool(explicit_value, default=False)
    provider_id = normalize_llm_provider(provider or active_llm_provider(config=config))
    preset = llm_provider_preset(provider_id)
    names = [str(preset.get("allow_insecure_tls_env") or "")]
    if provider_id == "openai_compatible":
        names.append("LLM_ALLOW_INSECURE_TLS")
    return parse_bool(_first_value(names, config=config, read_secret_fn=read_secret_fn), default=False)


def llm_urlopen(
    request: urllib.request.Request,
    *,
    timeout: int | float,
    provider: Any | None = None,
    allow_insecure_tls: Any | None = None,
):
    if resolve_llm_allow_insecure_tls(provider=provider, explicit_value=allow_insecure_tls):
        context = ssl._create_unverified_context()
        return urllib.request.urlopen(request, timeout=timeout, context=context)
    return urllib.request.urlopen(request, timeout=timeout)


def normalize_llm_base_url(value: str | None) -> str:
    text = str(value or "").strip().rstrip("/")
    lower = text.lower()
    for suffix in ("/chat/completions", "/models", "/messages"):
        if lower.endswith(suffix):
            text = text[: -len(suffix)].rstrip("/")
            lower = text.lower()
    return text


def llm_provider_request_style(provider: Any) -> str:
    preset = llm_provider_preset(provider)
    return str(preset.get("request_style") or "openai_chat").strip() or "openai_chat"


def llm_provider_supports_reasoning_effort(provider: Any) -> bool:
    return llm_provider_request_style(provider) == "openai_chat"


def resolve_llm_fallback_provider(*, config: dict[str, str] | None = None) -> str:
    payload = config if config is not None else load_llm_config()
    raw = payload.get("LLM_FALLBACK_PROVIDER") or os.getenv("LLM_FALLBACK_PROVIDER") or _read_registry_value("LLM_FALLBACK_PROVIDER")
    return normalize_llm_provider(raw) if raw else ""


def llm_fallback_enabled(*, config: dict[str, str] | None = None) -> bool:
    payload = config if config is not None else load_llm_config()
    value = payload.get("LLM_FALLBACK_ENABLED")
    if value is None:
        value = os.getenv("LLM_FALLBACK_ENABLED") or _read_registry_value("LLM_FALLBACK_ENABLED")
    return parse_bool(value, default=False)


def resolve_llm_fallback_settings(*, config: dict[str, str] | None = None, tier: str = "flash") -> dict[str, Any]:
    payload = config if config is not None else load_llm_config()
    provider = resolve_llm_fallback_provider(config=payload)
    if not provider:
        return {"enabled": False, "provider": ""}
    normalized_tier = normalize_deepseek_model_tier(tier)
    explicit_base_url = payload.get("LLM_FALLBACK_BASE_URL") or None
    explicit_model = (
        payload.get("LLM_FALLBACK_PRO_MODEL")
        if normalized_tier == "pro"
        else payload.get("LLM_FALLBACK_FLASH_MODEL")
    ) or payload.get("LLM_FALLBACK_MODEL") or None
    explicit_reasoning = (
        payload.get("LLM_FALLBACK_PRO_REASONING_EFFORT")
        if normalized_tier == "pro"
        else payload.get("LLM_FALLBACK_FLASH_REASONING_EFFORT")
    )
    explicit_key = str(payload.get("LLM_FALLBACK_API_KEY") or "").strip()
    allow_insecure_tls = parse_bool(
        payload.get("LLM_FALLBACK_ALLOW_INSECURE_TLS"),
        default=resolve_llm_allow_insecure_tls(provider=provider, config=payload),
    )
    return {
        "enabled": llm_fallback_enabled(config=payload),
        "provider": provider,
        "provider_label": str(llm_provider_preset(provider).get("label") or provider),
        "base_url": resolve_llm_base_url(provider=provider, explicit_base_url=explicit_base_url, config=payload),
        "model": resolve_llm_tier_model(provider=provider, tier=normalized_tier, explicit_model=explicit_model, config=payload),
        "flash_model": resolve_llm_tier_model(
            provider=provider,
            tier="flash",
            explicit_model=payload.get("LLM_FALLBACK_FLASH_MODEL") or payload.get("LLM_FALLBACK_MODEL") or None,
            config=payload,
        ),
        "pro_model": resolve_llm_tier_model(
            provider=provider,
            tier="pro",
            explicit_model=payload.get("LLM_FALLBACK_PRO_MODEL") or payload.get("LLM_FALLBACK_MODEL") or None,
            config=payload,
        ),
        "api_key": explicit_key or resolve_llm_api_key(provider=provider, config=payload),
        "flash_reasoning_effort": normalize_llm_reasoning_effort(
            payload.get("LLM_FALLBACK_FLASH_REASONING_EFFORT")
            or resolve_llm_reasoning_effort(provider=provider, tier="flash", config=payload)
        ),
        "pro_reasoning_effort": normalize_llm_reasoning_effort(
            payload.get("LLM_FALLBACK_PRO_REASONING_EFFORT")
            or resolve_llm_reasoning_effort(provider=provider, tier="pro", config=payload)
        ),
        "allow_insecure_tls": allow_insecure_tls,
        "request_style": llm_provider_request_style(provider),
    }


def llm_route_signature(*, provider: Any, base_url: str, model: str, api_key: str) -> tuple[str, str, str, str]:
    return (
        normalize_llm_provider(provider),
        normalize_llm_base_url(base_url),
        str(model or "").strip(),
        str(api_key or "").strip(),
    )


def is_transient_llm_failure(result: dict[str, Any] | None) -> bool:
    payload = result if isinstance(result, dict) else {}
    try:
        status = int(payload.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    if status in TRANSIENT_LLM_HTTP_STATUSES:
        return True
    text = " ".join(
        str(payload.get(key) or "")
        for key in ("error", "reason", "message", "detail")
    ).lower()
    return any(marker in text for marker in TRANSIENT_LLM_ERROR_MARKERS)


def is_failoverable_llm_failure(result: dict[str, Any] | None) -> bool:
    """Return True when a failed primary route is worth retrying on fallback."""

    if is_transient_llm_failure(result):
        return True
    payload = result if isinstance(result, dict) else {}
    text = " ".join(
        str(payload.get(key) or "")
        for key in ("error", "reason", "message", "detail")
    ).lower()
    if any(marker in text for marker in MODEL_UNAVAILABLE_LLM_ERROR_MARKERS):
        return True
    return any(marker in text for marker in GATEWAY_FAILOVERABLE_LLM_ERROR_MARKERS)


def extract_llm_response_text(*, provider: Any, data: Any) -> str:
    provider_id = normalize_llm_provider(provider)
    if llm_provider_request_style(provider_id) == "anthropic_messages":
        items = data.get("content") if isinstance(data, dict) else []
        texts: list[str] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or item.get("content") or "").strip()
                if text:
                    texts.append(text)
        return "\n".join(texts).strip()
    return str(((data.get("choices", [{}])[0] if isinstance(data, dict) else {}).get("message", {}) or {}).get("content", "") or "").strip()


def call_llm_request_once(
    *,
    provider: Any,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout: int | float,
    max_tokens: int,
    temperature: float | None = None,
    tier: str = "flash",
    json_mode: bool = False,
    explicit_reasoning_effort: Any | None = None,
    allow_insecure_tls: Any | None = None,
) -> dict[str, Any]:
    provider_id = normalize_llm_provider(provider)
    request_style = llm_provider_request_style(provider_id)
    if request_style == "anthropic_messages":
        system_parts: list[str] = []
        request_messages: list[dict[str, Any]] = []
        for message in messages or []:
            role = str((message or {}).get("role") or "user").strip().lower()
            content = str((message or {}).get("content") or "")
            if role == "system":
                if content:
                    system_parts.append(content)
                continue
            normalized_role = "assistant" if role == "assistant" else "user"
            request_messages.append({"role": normalized_role, "content": content})
        payload: dict[str, Any] = {
            "model": model,
            "messages": request_messages,
            "max_tokens": max(1, int(max_tokens)),
            "stream": False,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if temperature is not None:
            payload["temperature"] = float(temperature)
        url = normalize_llm_base_url(base_url).rstrip("/") + "/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    else:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max(1, int(max_tokens)),
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if llm_provider_supports_reasoning_effort(provider_id):
            apply_llm_reasoning_effort(
                payload,
                provider=provider_id,
                tier=tier,
                explicit_value=explicit_reasoning_effort,
            )
        url = normalize_llm_base_url(base_url).rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with llm_urlopen(
            request,
            timeout=max(1, timeout),
            provider=provider_id,
            allow_insecure_tls=allow_insecure_tls,
        ) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            return {
                "ok": True,
                "provider": provider_id,
                "provider_label": llm_route_display_label(provider=provider_id, model=model, request_style=request_style),
                "model": model,
                "base_url": normalize_llm_base_url(base_url),
                "status": int(getattr(response, "status", 200) or 200),
                "response_text": extract_llm_response_text(provider=provider_id, data=data),
                "usage": data.get("usage", {}) if isinstance(data, dict) else {},
                "request_style": request_style,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "provider": provider_id,
            "provider_label": llm_route_display_label(provider=provider_id, model=model, request_style=request_style),
            "model": model,
            "base_url": normalize_llm_base_url(base_url),
            "status": int(getattr(exc, "code", 0) or 0),
            "error": body[:1000],
            "request_style": request_style,
        }
    except Exception as exc:  # noqa: BLE001 - caller converts to user-visible diagnostics
        return {
            "ok": False,
            "provider": provider_id,
            "provider_label": llm_route_display_label(provider=provider_id, model=model, request_style=request_style),
            "model": model,
            "base_url": normalize_llm_base_url(base_url),
            "status": 0,
            "error": repr(exc),
            "request_style": request_style,
        }


def call_llm_request_once_with_wall_timeout(
    *,
    wall_timeout: int | float | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if wall_timeout is None or float(wall_timeout) <= 0:
        return call_llm_request_once(**kwargs)
    result_holder: dict[str, Any] = {}
    finished = threading.Event()

    def worker() -> None:
        try:
            result_holder["result"] = call_llm_request_once(**kwargs)
        except Exception as exc:  # noqa: BLE001 - convert late worker errors to diagnostics
            result_holder["result"] = {"ok": False, "status": 0, "error": repr(exc)}
        finally:
            finished.set()

    thread = threading.Thread(target=worker, name="llm-wall-timeout", daemon=True)
    thread.start()
    if finished.wait(max(1, float(wall_timeout))):
        result = result_holder.get("result")
        return result if isinstance(result, dict) else {"ok": False, "status": 0, "error": "llm_wall_timeout_missing_result"}
    return {
        "ok": False,
        "status": 0,
        "error": f"llm_wall_timeout_after_{max(1, float(wall_timeout)):.1f}s",
        "wall_timeout": True,
        "wall_timeout_seconds": max(1, float(wall_timeout)),
    }


def call_llm_request_with_failover(
    *,
    provider: Any,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout: int | float,
    max_tokens: int,
    fallback_timeout: int | float | None = None,
    temperature: float | None = None,
    tier: str = "flash",
    json_mode: bool = False,
    explicit_reasoning_effort: Any | None = None,
    allow_insecure_tls: Any | None = None,
    allow_fallback: bool = True,
    wall_timeout: int | float | None = None,
    fallback_wall_timeout: int | float | None = None,
    config: dict[str, str] | None = None,
) -> dict[str, Any]:
    primary = call_llm_request_once_with_wall_timeout(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        messages=messages,
        timeout=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
        tier=tier,
        json_mode=json_mode,
        explicit_reasoning_effort=explicit_reasoning_effort,
        allow_insecure_tls=allow_insecure_tls,
        wall_timeout=wall_timeout,
    )
    if primary.get("ok"):
        primary["failover"] = {"attempted": False, "activated": False, "reason": "primary_ok"}
        return primary
    if not allow_fallback:
        primary["failover"] = {"attempted": False, "activated": False, "reason": "fallback_disallowed_for_stage"}
        return primary
    fallback = resolve_llm_fallback_settings(config=config, tier=tier)
    if not fallback.get("enabled"):
        primary["failover"] = {"attempted": False, "activated": False, "reason": "fallback_disabled"}
        return primary
    if not is_failoverable_llm_failure(primary):
        primary["failover"] = {"attempted": False, "activated": False, "reason": "primary_error_not_transient"}
        return primary
    fallback_provider = str(fallback.get("provider") or "").strip()
    fallback_api_key = str(fallback.get("api_key") or "").strip()
    fallback_base_url = str(fallback.get("base_url") or "").strip()
    fallback_model = str(fallback.get("model") or "").strip()
    if not (fallback_provider and fallback_api_key and fallback_base_url and fallback_model):
        primary["failover"] = {"attempted": False, "activated": False, "reason": "fallback_not_ready"}
        return primary
    if llm_route_signature(provider=provider, base_url=base_url, model=model, api_key=api_key) == llm_route_signature(
        provider=fallback_provider,
        base_url=fallback_base_url,
        model=fallback_model,
        api_key=fallback_api_key,
    ):
        primary["failover"] = {"attempted": False, "activated": False, "reason": "fallback_same_as_primary"}
        return primary
    fallback_result = call_llm_request_once_with_wall_timeout(
        provider=fallback_provider,
        api_key=fallback_api_key,
        base_url=fallback_base_url,
        model=fallback_model,
        messages=messages,
        timeout=max(1, fallback_timeout if fallback_timeout is not None else timeout),
        max_tokens=max_tokens,
        temperature=temperature,
        tier=tier,
        json_mode=json_mode,
        explicit_reasoning_effort=(
            fallback.get("pro_reasoning_effort")
            if normalize_deepseek_model_tier(tier) == "pro"
            else fallback.get("flash_reasoning_effort")
        ),
        allow_insecure_tls=fallback.get("allow_insecure_tls"),
        wall_timeout=fallback_wall_timeout if fallback_wall_timeout is not None else fallback_timeout,
    )
    if fallback_result.get("ok"):
        fallback_result["failover"] = {
            "attempted": True,
            "activated": True,
            "reason": "fallback_success",
            "primary_provider": normalize_llm_provider(provider),
            "primary_provider_label": str(primary.get("provider_label") or llm_route_display_label(
                provider=provider,
                model=primary.get("model") or model,
                request_style=primary.get("request_style"),
            )),
            "primary_status": primary.get("status", 0),
            "primary_error": str(primary.get("error") or ""),
            "fallback_provider": fallback_provider,
            "fallback_provider_label": str(fallback_result.get("provider_label") or llm_route_display_label(
                provider=fallback_provider,
                model=fallback_result.get("model") or fallback_model,
                request_style=fallback_result.get("request_style"),
            )),
            "fallback_timeout_seconds": max(1, fallback_timeout if fallback_timeout is not None else timeout),
        }
        return fallback_result
    primary["failover"] = {
        "attempted": True,
        "activated": False,
        "reason": "fallback_failed",
        "primary_provider": normalize_llm_provider(provider),
        "primary_provider_label": str(primary.get("provider_label") or llm_route_display_label(
            provider=provider,
            model=primary.get("model") or model,
            request_style=primary.get("request_style"),
        )),
        "primary_status": primary.get("status", 0),
        "primary_error": str(primary.get("error") or ""),
        "fallback_provider": fallback_provider,
        "fallback_provider_label": str(fallback_result.get("provider_label") or llm_route_display_label(
            provider=fallback_provider,
            model=fallback_result.get("model") or fallback_model,
            request_style=fallback_result.get("request_style"),
        )),
        "fallback_timeout_seconds": max(1, fallback_timeout if fallback_timeout is not None else timeout),
        "fallback_status": fallback_result.get("status", 0),
        "fallback_error": str(fallback_result.get("error") or ""),
    }
    return primary


def _legacy_provider_for_reader(read_secret_fn: SecretReader) -> str:
    if read_secret_fn is read_secret:
        return active_llm_provider()
    return DEFAULT_LLM_PROVIDER


def _first_value(
    names: list[str],
    *,
    config: dict[str, str] | None = None,
    read_secret_fn: SecretReader | None = None,
) -> str:
    for name in names:
        if not name:
            continue
        if read_secret_fn is not None and read_secret_fn is not read_secret:
            value = str(read_secret_fn(name) or "").strip()
        else:
            value = _read_named_value(name, config=config)
        if value:
            return value
    return ""


def _read_named_value(name: str, *, config: dict[str, str] | None = None) -> str:
    if not name:
        return ""
    payload = config if config is not None else load_llm_config()
    value = payload.get(name)
    if value:
        return value
    value = os.getenv(name)
    if value:
        return value
    return _read_registry_value(name)


def _read_registry_value(name: str) -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            registry_value, _ = winreg.QueryValueEx(key, name)
            return str(registry_value)
    except Exception:
        return ""


def resolve_deepseek_model(
    *,
    explicit_model: str | None = None,
    read_secret_fn: SecretReader = read_secret,
) -> str:
    return (
        resolve_llm_model(
            provider=_legacy_provider_for_reader(read_secret_fn),
            explicit_model=explicit_model,
            read_secret_fn=read_secret_fn,
        )
        or DEFAULT_DEEPSEEK_MODEL
    )


def resolve_deepseek_tier_model(
    *,
    tier: str,
    explicit_model: str | None = None,
    read_secret_fn: SecretReader = read_secret,
) -> str:
    """Resolve the model for a quality tier.

    `DEEPSEEK_MODEL` remains a legacy global override. Flash and Pro can be
    configured independently with `DEEPSEEK_FLASH_MODEL` and
    `DEEPSEEK_PRO_MODEL` so cost routing does not accidentally collapse back to
    one global model.
    """
    normalized = normalize_deepseek_model_tier(tier)
    default = DEFAULT_DEEPSEEK_FLASH_MODEL if normalized == "flash" else DEFAULT_DEEPSEEK_PRO_MODEL
    return (
        resolve_llm_tier_model(
            provider=_legacy_provider_for_reader(read_secret_fn),
            tier=normalized,
            explicit_model=explicit_model,
            read_secret_fn=read_secret_fn,
        )
        or default
    )


def normalize_deepseek_model_tier(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in {"flash", "fast", "cheap", "economy", "lite"}:
        return "flash"
    if text in {"pro", "quality", "reasoning", "deep"}:
        return "pro"
    return "flash"


def resolve_deepseek_base_url(
    *,
    explicit_base_url: str | None = None,
    read_secret_fn: SecretReader = read_secret,
) -> str:
    return (
        resolve_llm_base_url(
            provider=_legacy_provider_for_reader(read_secret_fn),
            explicit_base_url=explicit_base_url,
            read_secret_fn=read_secret_fn,
        )
        or DEFAULT_DEEPSEEK_BASE_URL
    )


def resolve_deepseek_max_tokens(
    default: int,
    *,
    read_secret_fn: SecretReader = read_secret,
) -> int:
    return positive_int(read_secret_fn("DEEPSEEK_MAX_TOKENS"), default)


def resolve_deepseek_timeout(
    default: int = DEFAULT_DEEPSEEK_TIMEOUT_SECONDS,
    *,
    read_secret_fn: SecretReader = read_secret,
) -> int:
    return positive_int(read_secret_fn("DEEPSEEK_TIMEOUT_SECONDS"), default)


def positive_int(value: str | int | None, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return max(1, int(default))
    return parsed if parsed > 0 else max(1, int(default))


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "y", "on", "allow", "allowed", "insecure"}:
        return True
    if text in {"0", "false", "no", "n", "off", "deny", "denied", "secure"}:
        return False
    return bool(default)
