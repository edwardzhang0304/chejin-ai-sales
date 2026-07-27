from __future__ import annotations

import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from types import SimpleNamespace

from app.core.config import Settings
from app.errors import AppError
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def production_settings(**overrides):
    return Settings(
        environment="production",
        auto_create_tables=False,
        phone_hash_secret="production-phone-hash-secret",
        contact_encryption_secret="production-contact-encryption-secret",
        **overrides,
    )


def test_production_rejects_mock_brain_adapter():
    with pytest.raises(ValidationError, match="C3_AI_ADAPTER_MODE=real"):
        production_settings(c3_ai_adapter_mode="mock")


def test_production_accepts_real_brain_adapter():
    settings = production_settings(c3_ai_adapter_mode="real")

    assert settings.is_production is True
    assert settings.c3_ai_adapter_mode == "real"


def test_batch_stale_window_must_exceed_provider_timeout():
    with pytest.raises(ValidationError, match="批次失效时间"):
        Settings(
            c3_brain_provider_timeout_seconds=180,
            c3_batch_stale_after_seconds=180,
        )


def test_real_brain_readiness_rejects_missing_api_key(monkeypatch):
    monkeypatch.setattr(
        "app.services.ai_adapter.importlib.import_module",
        lambda _name: SimpleNamespace(resolve_llm_api_key=lambda **_kwargs: ""),
    )

    with pytest.raises(AppError) as exc:
        RealOmniAutoAIEngineAdapter._require_api_key(
            {"customer_service_brain": {"provider": "openai", "model": "test"}}
        )

    assert exc.value.code == "AI_ENGINE_API_KEY_MISSING"


def test_real_brain_readiness_accepts_explicit_runtime_key_without_exposing_it():
    RealOmniAutoAIEngineAdapter._require_api_key(
        {
            "customer_service_brain": {
                "provider": "openai",
                "model": "test",
                "api_key": "test-only-not-a-real-secret",
            }
        }
    )


def test_packaged_brain_config_uses_runtime_secret_only():
    config = json.loads(
        (PROJECT_ROOT / "backend" / "configs" / "chejin_c3_brain.json").read_text(
            encoding="utf-8"
        )
    )
    brain = config["customer_service_brain"]

    assert brain["enabled"] is True
    assert brain["mode"] == "brain_first"
    assert brain["provider"] == "openai"
    assert brain["model"] == "gpt-5.5"
    assert brain["base_url"] == "https://aiself.vip/v1"
    assert "api_key" not in brain
    assert "doubao" not in json.dumps(brain).lower()


def test_settings_env_file_allows_provider_owned_variables(tmp_path, monkeypatch):
    monkeypatch.delenv("C3_AI_ADAPTER_MODE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "C3_AI_ADAPTER_MODE=real",
                "OPENAI_API_KEY=test-only-token",
                "OPENAI_BASE_URL=https://aiself.vip/v1",
                "OPENAI_MODEL=gpt-5.5",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.c3_ai_adapter_mode == "real"


def test_env_example_declares_one_formal_brain_route_and_compose_injects_env_file():
    lines = (PROJECT_ROOT / "backend" / ".env.example").read_text(encoding="utf-8").splitlines()
    assignments = [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    ]
    keys = [line.split("=", 1)[0] for line in assignments]

    assert len(keys) == len(set(keys))
    assert "OPENAI_API_KEY" in keys
    assert "OPENAI_MODEL" in keys
    assert "OPENAI_FLASH_REASONING_EFFORT" in keys
    assert "ANTHROPIC_AUTH_TOKEN" not in keys
    compose = (PROJECT_ROOT / "backend" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "env_file:" in compose
    assert "path: .env" in compose
