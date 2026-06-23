"""Focused checks for multi-provider LLM configuration."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")
os.environ.setdefault("WECHAT_VPS_BASE_URL", "http://localhost:8000")
os.environ.setdefault("WECHAT_CLOUD_REQUIRE_NODE_VERIFIED", "0")

from apps.wechat_ai_customer_service import llm_config as llm_config_module  # noqa: E402
from apps.wechat_ai_customer_service.auth.models import AuthContext, AuthSession, AuthUser, Role  # noqa: E402
from apps.wechat_ai_customer_service.auth.permissions import can_access  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.app import create_app  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.auth_context import action_for_request, resource_for_path  # noqa: E402


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value: Any, message: str) -> None:
    if not value:
        raise AssertionError(message)


class FakeResponse:
    status = 200

    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self.body = body or {"choices": [{"message": {"content": "OK"}}]}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.body).encode("utf-8")


def run_checks() -> dict[str, Any]:
    old_path = llm_config_module._LLM_CONFIG_PATH
    old_urlopen = llm_config_module.urllib.request.urlopen
    calls: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="omniauto-llm-config-") as temp_dir:
        llm_config_module._LLM_CONFIG_PATH = Path(temp_dir) / "llm_config.json"

        def fake_urlopen(request: Any, timeout: int = 0, **kwargs: Any) -> FakeResponse:
            headers = {str(key).lower(): value for key, value in request.header_items()}
            calls.append(
                {
                    "url": request.full_url,
                    "headers": headers,
                    "body": json.loads(request.data.decode("utf-8")) if request.data else None,
                    "timeout": timeout,
                    "kwargs": kwargs,
                }
            )
            if "openai.invalid" in str(request.full_url):
                raise TimeoutError("simulated primary timeout")
            request_url = str(request.full_url)
            if request_url.endswith("/models"):
                if "x-api-key" in headers:
                    return FakeResponse({"data": [{"id": "kimi-for-coding"}]})
                if "aiself.vip" in request_url or "deepseek" in request_url:
                    return FakeResponse(
                        {"data": [{"id": "deepseek-v4-flash"}, {"id": "deepseek-v4-pro"}, {"id": "gpt-5.4"}]}
                    )
                return FakeResponse({"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4.1"}]})
            if request_url.endswith("/messages"):
                return FakeResponse({"content": [{"type": "text", "text": "OK"}]})
            return FakeResponse()

        llm_config_module.urllib.request.urlopen = fake_urlopen
        try:
            check_legacy_deepseek_defaults()
            check_openai_compatible_roundtrip_and_probe(calls)
            check_deepseek_gateway_roundtrip_and_probe(calls)
            check_fallback_roundtrip_and_anthropic_probe(calls)
            check_kimi_primary_deepseek_flash_fallback_payload_and_failover(calls)
            check_model_unavailable_primary_is_failoverable()
            check_stage_can_disallow_fallback(calls)
            check_failover_preserves_actual_fallback_route(calls)
            check_wall_timeout_primary_activates_fallback()
            check_llm_config_permissions_allow_all_authenticated_users()
        finally:
            llm_config_module.urllib.request.urlopen = old_urlopen
            llm_config_module._LLM_CONFIG_PATH = old_path
    return {"ok": True, "checks": 10}


def check_legacy_deepseek_defaults() -> None:
    llm_config_module.save_llm_config({})
    assert_equal(
        llm_config_module.resolve_deepseek_tier_model(tier="flash", read_secret_fn=lambda name: ""),
        "deepseek-v4-flash",
        "legacy DeepSeek flash default should stay stable",
    )
    assert_equal(
        llm_config_module.resolve_deepseek_tier_model(tier="pro", read_secret_fn=lambda name: ""),
        "deepseek-v4-pro",
        "legacy DeepSeek pro default should stay stable",
    )


def check_openai_compatible_roundtrip_and_probe(calls: list[dict[str, Any]]) -> None:
    client = TestClient(create_app())
    response = client.put(
        "/api/system/llm-config",
        json={
            "provider": "openai_compatible",
            "base_url": "https://relay.example/v1/chat/completions",
            "flash_model": "gpt-4o-mini",
            "pro_model": "gpt-4.1",
            "flash_reasoning_effort": "low",
            "pro_reasoning_effort": "high",
            "allow_insecure_tls": True,
            "api_key": "sk-test-provider",
        },
    )
    assert_equal(response.status_code, 200, "save status")
    saved = response.json()
    assert_true(saved.get("ok"), "save should succeed")
    assert_equal(saved.get("provider"), "openai_compatible", "provider should be saved")
    assert_equal(saved.get("base_url"), "https://relay.example/v1", "base URL should be normalized")
    assert_equal(saved.get("flash_model"), "gpt-4o-mini", "flash model should roundtrip")
    assert_equal(saved.get("pro_model"), "gpt-4.1", "pro model should roundtrip")
    assert_equal(saved.get("flash_reasoning_effort"), "low", "flash reasoning effort should roundtrip")
    assert_equal(saved.get("pro_reasoning_effort"), "high", "pro reasoning effort should roundtrip")
    assert_true("medium" in saved.get("reasoning_effort_options", []), "reasoning effort options should be exposed")
    assert_equal(saved.get("available_models"), ["gpt-4o-mini", "gpt-4.1"], "live model options should be exposed")
    assert_true(saved.get("allow_insecure_tls"), "insecure TLS option should roundtrip")
    assert_true(saved.get("api_key_configured"), "API key should be recorded as configured")
    assert_true("sk-test-provider" not in json.dumps(saved), "saved payload must not leak raw API key")

    assert_equal(llm_config_module.read_secret("DEEPSEEK_API_KEY"), "sk-test-provider", "legacy key read should map to active provider")
    assert_equal(llm_config_module.resolve_deepseek_base_url(), "https://relay.example/v1", "legacy base URL should map to active provider")
    assert_equal(
        llm_config_module.resolve_deepseek_tier_model(tier="flash"),
        "gpt-4o-mini",
        "legacy flash model should map to active provider",
    )

    probe = client.post("/api/system/llm-config/test", json={})
    assert_equal(probe.status_code, 200, "probe status")
    payload = probe.json()
    assert_true(payload.get("ok"), "probe should succeed through fake urlopen")
    assert_equal(payload.get("provider"), "openai_compatible", "probe should use active provider")
    assert_equal(calls[-1]["url"], "https://relay.example/v1/chat/completions", "probe should call chat completions")
    assert_equal(calls[-1]["body"]["model"], "gpt-4o-mini", "probe should use flash model")
    assert_equal(calls[-1]["body"]["reasoning_effort"], "low", "probe should send flash reasoning effort")
    assert_equal(calls[-1]["headers"].get("authorization"), "Bearer sk-test-provider", "probe should send bearer key")

    pro_probe = client.post("/api/system/llm-config/test", json={"route": "pro"})
    assert_equal(pro_probe.status_code, 200, "pro probe status")
    pro_payload = pro_probe.json()
    assert_true(pro_payload.get("ok"), "pro probe should succeed through fake urlopen")
    assert_equal(calls[-1]["body"]["model"], "gpt-4.1", "pro probe should use pro model")
    assert_equal(calls[-1]["body"]["reasoning_effort"], "high", "pro probe should send pro reasoning effort")


def check_deepseek_gateway_roundtrip_and_probe(calls: list[dict[str, Any]]) -> None:
    client = TestClient(create_app())
    response = client.put(
        "/api/system/llm-config",
        json={
            "provider": "deepseek",
            "base_url": "https://aiself.vip/v1/chat/completions",
            "flash_model": "deepseek-v4-flash",
            "pro_model": "deepseek-v4-pro",
            "flash_reasoning_effort": "",
            "pro_reasoning_effort": "",
            "allow_insecure_tls": False,
            "api_key": "sk-deepseek-gateway",
        },
    )
    assert_equal(response.status_code, 200, "DeepSeek gateway save status")
    saved = response.json()
    assert_true(saved.get("ok"), "DeepSeek gateway save should succeed")
    assert_equal(saved.get("provider"), "deepseek", "DeepSeek provider should be saved")
    assert_equal(saved.get("base_url"), "https://aiself.vip/v1", "DeepSeek gateway base URL should be normalized")
    assert_equal(saved.get("flash_model"), "deepseek-v4-flash", "DeepSeek flash model should roundtrip")
    assert_equal(saved.get("pro_model"), "deepseek-v4-pro", "DeepSeek pro model should roundtrip")
    assert_equal(
        saved.get("available_models"),
        ["deepseek-v4-flash", "deepseek-v4-pro", "gpt-5.4"],
        "DeepSeek gateway live model options should be exposed",
    )
    assert_true(saved.get("api_key_configured"), "DeepSeek API key should be recorded as configured")
    assert_true("sk-deepseek-gateway" not in json.dumps(saved), "DeepSeek save payload must not leak raw API key")

    assert_equal(llm_config_module.active_llm_provider(), "deepseek", "active provider should switch to DeepSeek")
    assert_equal(llm_config_module.resolve_deepseek_base_url(), "https://aiself.vip/v1", "legacy DeepSeek base URL should map to gateway")
    assert_equal(
        llm_config_module.resolve_deepseek_tier_model(tier="flash"),
        "deepseek-v4-flash",
        "legacy DeepSeek flash model should map to configured flash route",
    )
    assert_equal(
        llm_config_module.resolve_deepseek_tier_model(tier="pro"),
        "deepseek-v4-pro",
        "legacy DeepSeek pro model should map to configured pro route",
    )

    probe = client.post("/api/system/llm-config/test", json={})
    assert_equal(probe.status_code, 200, "DeepSeek flash probe status")
    payload = probe.json()
    assert_true(payload.get("ok"), "DeepSeek flash probe should succeed through fake urlopen")
    assert_equal(payload.get("provider"), "deepseek", "DeepSeek flash probe should use active provider")
    assert_equal(calls[-1]["url"], "https://aiself.vip/v1/chat/completions", "DeepSeek flash probe should call gateway chat completions")
    assert_equal(calls[-1]["body"]["model"], "deepseek-v4-flash", "DeepSeek flash probe should use flash model")
    assert_equal(calls[-1]["headers"].get("authorization"), "Bearer sk-deepseek-gateway", "DeepSeek flash probe should send bearer key")

    pro_probe = client.post("/api/system/llm-config/test", json={"route": "pro"})
    assert_equal(pro_probe.status_code, 200, "DeepSeek pro probe status")
    pro_payload = pro_probe.json()
    assert_true(pro_payload.get("ok"), "DeepSeek pro probe should succeed through fake urlopen")
    assert_equal(calls[-1]["url"], "https://aiself.vip/v1/chat/completions", "DeepSeek pro probe should call gateway chat completions")
    assert_equal(calls[-1]["body"]["model"], "deepseek-v4-pro", "DeepSeek pro probe should use pro model")


def check_fallback_roundtrip_and_anthropic_probe(calls: list[dict[str, Any]]) -> None:
    client = TestClient(create_app())
    response = client.put(
        "/api/system/llm-config",
        json={
            "fallback_enabled": True,
            "fallback_provider": "anthropic",
            "fallback_base_url": "https://aiself.vip/v1/messages",
            "fallback_flash_model": "kimi-for-coding",
            "fallback_pro_model": "kimi-for-coding",
            "fallback_api_key": "sk-fallback-kimi",
        },
    )
    assert_equal(response.status_code, 200, "fallback save status")
    saved = response.json()
    fallback = saved.get("fallback") if isinstance(saved.get("fallback"), dict) else {}
    assert_true(fallback.get("enabled") is True, "fallback should be enabled after save")
    assert_equal(fallback.get("provider"), "anthropic", "fallback provider should roundtrip")
    assert_equal(fallback.get("base_url"), "https://aiself.vip/v1", "fallback base URL should be normalized")
    assert_equal(fallback.get("flash_model"), "kimi-for-coding", "fallback flash model should roundtrip")
    assert_equal(fallback.get("pro_model"), "kimi-for-coding", "fallback pro model should roundtrip")
    assert_true(fallback.get("api_key_configured"), "fallback API key should be marked configured")
    assert_true("sk-fallback-kimi" not in json.dumps(saved), "fallback save payload must not leak raw key")

    probe = client.post("/api/system/llm-config/test", json={"target": "fallback"})
    assert_equal(probe.status_code, 200, "fallback probe status")
    payload = probe.json()
    assert_true(payload.get("ok"), "fallback probe should succeed through fake urlopen")
    assert_equal(payload.get("provider"), "anthropic", "fallback probe should use anthropic provider")
    assert_equal(calls[-1]["url"], "https://aiself.vip/v1/messages", "fallback probe should call anthropic messages endpoint")
    assert_equal(calls[-1]["body"]["model"], "kimi-for-coding", "fallback probe should use fallback flash model")
    assert_equal(calls[-1]["headers"].get("x-api-key"), "sk-fallback-kimi", "fallback probe should send anthropic API key header")
    assert_equal(calls[-1]["headers"].get("anthropic-version"), "2023-06-01", "fallback probe should send anthropic version header")


def check_kimi_primary_deepseek_flash_fallback_payload_and_failover(calls: list[dict[str, Any]]) -> None:
    client = TestClient(create_app())
    response = client.put(
        "/api/system/llm-config",
        json={
            "provider": "anthropic",
            "base_url": "https://aiself.vip/v1/messages",
            "flash_model": "kimi-for-coding",
            "pro_model": "kimi-for-coding",
            "api_key": "sk-primary-kimi",
            "fallback_enabled": True,
            "fallback_provider": "deepseek",
            "fallback_base_url": "https://aiself.vip/v1/chat/completions",
            "fallback_flash_model": "deepseek-v4-flash",
            "fallback_pro_model": "deepseek-v4-flash",
            "fallback_api_key": "sk-fallback-deepseek",
        },
    )
    assert_equal(response.status_code, 200, "Kimi primary / DeepSeek fallback save status")
    saved = response.json()
    fallback = saved.get("fallback") if isinstance(saved.get("fallback"), dict) else {}
    assert_equal(saved.get("provider"), "anthropic", "primary provider should be Kimi-compatible Anthropic")
    assert_equal(saved.get("flash_model"), "kimi-for-coding", "primary flash model should be Kimi")
    assert_equal(saved.get("adapter_profile"), "kimi_anthropic_messages", "primary adapter should identify Kimi")
    assert_equal(fallback.get("provider"), "deepseek", "fallback provider should be DeepSeek")
    assert_equal(fallback.get("flash_model"), "deepseek-v4-flash", "fallback flash model should be DeepSeek v4 Flash")
    assert_equal(fallback.get("pro_model"), "deepseek-v4-flash", "fallback pro model should also be DeepSeek v4 Flash")
    assert_equal(fallback.get("adapter_profile"), "deepseek_v4_flash_fallback", "fallback adapter should identify DeepSeek Flash")
    assert_true("sk-primary-kimi" not in json.dumps(saved), "primary raw key must not leak")
    assert_true("sk-fallback-deepseek" not in json.dumps(saved), "fallback raw key must not leak")

    before = len(calls)
    result = llm_config_module.call_llm_request_with_failover(
        provider="openai",
        api_key="sk-primary",
        base_url="https://openai.invalid/v1",
        model="gpt-5.4",
        messages=[{"role": "user", "content": "ping"}],
        timeout=1,
        max_tokens=8,
        fallback_timeout=30,
        config={
            "LLM_FALLBACK_ENABLED": "1",
            "LLM_FALLBACK_PROVIDER": "deepseek",
            "LLM_FALLBACK_BASE_URL": "https://aiself.vip/v1",
            "LLM_FALLBACK_FLASH_MODEL": "deepseek-v4-flash",
            "LLM_FALLBACK_PRO_MODEL": "deepseek-v4-flash",
            "LLM_FALLBACK_API_KEY": "sk-fallback-deepseek",
        },
    )
    assert_true(result.get("ok"), "DeepSeek Flash fallback should succeed through fake urlopen")
    assert_equal(result.get("provider"), "deepseek", "fallback result should report DeepSeek provider")
    assert_equal(result.get("model"), "deepseek-v4-flash", "fallback result should report DeepSeek Flash model")
    assert_true(len(calls) >= before + 2, "failover should call primary and fallback")
    assert_equal(calls[-1]["url"], "https://aiself.vip/v1/chat/completions", "DeepSeek fallback should call chat completions")
    assert_equal(calls[-1]["body"]["model"], "deepseek-v4-flash", "DeepSeek fallback request should use flash model")
    assert_equal(calls[-1]["headers"].get("authorization"), "Bearer sk-fallback-deepseek", "DeepSeek fallback should send bearer key")


def check_model_unavailable_primary_is_failoverable() -> None:
    unsupported_model = {
        "ok": False,
        "status": 400,
        "error": "{\"error\":{\"message\":\"The 'gpt-5.4' model is not supported when using Codex with a ChatGPT account.\"}}",
    }
    bad_request = {
        "ok": False,
        "status": 400,
        "error": "bad request: missing required messages",
    }
    assert_true(
        llm_config_module.is_failoverable_llm_failure(unsupported_model),
        "unsupported model primary failures should be allowed to use fallback",
    )
    assert_true(
        not llm_config_module.is_failoverable_llm_failure(bad_request),
        "ordinary malformed 400 requests should not be treated as failoverable",
    )


def check_stage_can_disallow_fallback(calls: list[dict[str, Any]]) -> None:
    before = len(calls)
    result = llm_config_module.call_llm_request_with_failover(
        provider="openai",
        api_key="sk-primary",
        base_url="https://openai.invalid/v1",
        model="gpt-5.4",
        messages=[{"role": "user", "content": "ping"}],
        timeout=1,
        max_tokens=8,
        allow_fallback=False,
        config={
            "LLM_FALLBACK_ENABLED": "1",
            "LLM_FALLBACK_PROVIDER": "anthropic",
            "LLM_FALLBACK_BASE_URL": "https://aiself.vip/v1",
            "LLM_FALLBACK_FLASH_MODEL": "kimi-for-coding",
            "LLM_FALLBACK_API_KEY": "sk-fallback-kimi",
        },
    )
    assert_true(not result.get("ok"), "disallowed fallback primary failure should stay failed")
    assert_equal(
        (result.get("failover") or {}).get("reason"),
        "fallback_disallowed_for_stage",
        "disallowed fallback should record stage reason",
    )
    assert_equal(len(calls), before + 1, "disallowed fallback should only call primary route")
    assert_true(calls[-1]["url"].endswith("/chat/completions"), "disallowed fallback should not call anthropic messages")


def check_failover_preserves_actual_fallback_route(calls: list[dict[str, Any]]) -> None:
    result = llm_config_module.call_llm_request_with_failover(
        provider="openai",
        api_key="sk-primary",
        base_url="https://openai.invalid/v1",
        model="gpt-5.4",
        messages=[{"role": "user", "content": "ping"}],
        timeout=1,
        max_tokens=8,
        fallback_timeout=60,
        config={
            "LLM_FALLBACK_ENABLED": "1",
            "LLM_FALLBACK_PROVIDER": "anthropic",
            "LLM_FALLBACK_BASE_URL": "https://aiself.vip/v1",
            "LLM_FALLBACK_FLASH_MODEL": "kimi-for-coding",
            "LLM_FALLBACK_API_KEY": "sk-fallback-kimi",
        },
    )
    assert_true(result.get("ok"), "fallback should succeed through fake urlopen")
    assert_equal(result.get("provider"), "anthropic", "fallback result should report actual provider")
    assert_equal(result.get("model"), "kimi-for-coding", "fallback result should report actual fallback model")
    assert_equal(
        (result.get("failover") or {}).get("fallback_timeout_seconds"),
        60,
        "fallback should use independent fallback timeout",
    )
    assert_equal(calls[-1]["url"], "https://aiself.vip/v1/messages", "fallback route should call anthropic messages")


def check_wall_timeout_primary_activates_fallback() -> None:
    old_urlopen = llm_config_module.urllib.request.urlopen
    calls: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: int = 0, **kwargs: Any) -> FakeResponse:
        headers = {str(key).lower(): value for key, value in request.header_items()}
        calls.append(
            {
                "url": request.full_url,
                "headers": headers,
                "body": json.loads(request.data.decode("utf-8")) if request.data else None,
                "timeout": timeout,
                "kwargs": kwargs,
            }
        )
        if "slow-primary.invalid" in str(request.full_url):
            time.sleep(2.0)
            return FakeResponse()
        return FakeResponse()

    llm_config_module.urllib.request.urlopen = fake_urlopen
    started = time.time()
    try:
        result = llm_config_module.call_llm_request_with_failover(
            provider="openai",
            api_key="sk-primary",
            base_url="https://slow-primary.invalid/v1",
            model="gpt-5.4",
            messages=[{"role": "user", "content": "ping"}],
            timeout=30,
            wall_timeout=0.2,
            max_tokens=8,
            fallback_timeout=30,
            fallback_wall_timeout=1,
            config={
                "LLM_FALLBACK_ENABLED": "1",
                "LLM_FALLBACK_PROVIDER": "deepseek",
                "LLM_FALLBACK_BASE_URL": "https://aiself.vip/v1",
                "LLM_FALLBACK_FLASH_MODEL": "deepseek-v4-flash",
                "LLM_FALLBACK_API_KEY": "sk-fallback-deepseek",
            },
        )
    finally:
        llm_config_module.urllib.request.urlopen = old_urlopen
    elapsed = time.time() - started
    assert_true(result.get("ok"), f"fallback should recover wall-timeout primary: {result}")
    assert_equal(result.get("provider"), "deepseek", "fallback result should report DeepSeek after wall timeout")
    assert_true(elapsed < 1.5, f"wall timeout should not wait for slow primary to finish, elapsed={elapsed:.3f}")
    assert_true(len(calls) >= 2, f"primary and fallback calls should both be attempted: {calls}")


def check_llm_config_permissions_allow_all_authenticated_users() -> None:
    assert_equal(resource_for_path("/api/system/llm-config"), "llm_config", "llm config route should use relaxed resource")
    assert_equal(resource_for_path("/api/system/llm-config/test"), "llm_config", "llm config test route should use relaxed resource")
    assert_equal(action_for_request("/api/system/llm-config", "PUT"), "write", "llm config save is a write action")
    assert_equal(action_for_request("/api/system/llm-config/test", "POST"), "write", "llm config test is a write action")
    for role in (Role.ADMIN, Role.CUSTOMER, Role.GUEST):
        context = AuthContext(
            session=AuthSession(
                session_id=f"{role.value}_session",
                user=AuthUser(user_id=f"{role.value}_user", role=role),
            ),
            tenant_id="default",
            authenticated=True,
        )
        assert_true(can_access(context, resource="llm_config", action="read"), f"{role.value} can read llm config")
        assert_true(can_access(context, resource="llm_config", action="write"), f"{role.value} can write llm config")
    guest_context = AuthContext(
        session=AuthSession(
            session_id="local_guest_session",
            user=AuthUser(user_id="local_guest_user", role=Role.GUEST),
        ),
        tenant_id="default",
        authenticated=False,
    )
    assert_true(can_access(guest_context, resource="llm_config", action="write"), "local implicit/dev users can write llm config")


def main() -> int:
    try:
        result = run_checks()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": repr(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
