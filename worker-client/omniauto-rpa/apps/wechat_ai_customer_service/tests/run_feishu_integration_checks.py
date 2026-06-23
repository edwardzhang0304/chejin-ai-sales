"""Focused checks for Feishu handoff notification integration."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("WECHAT_CLOUD_REQUIRED", "0")
os.environ.setdefault("WECHAT_CLOUD_STRICT_ONLINE", "0")
os.environ.setdefault("WECHAT_VPS_BASE_URL", "http://localhost:8000")
os.environ.setdefault("WECHAT_CLOUD_REQUIRE_NODE_VERIFIED", "0")

from apps.wechat_ai_customer_service.admin_backend.services import feishu_integration as feishu  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.app import create_app  # noqa: E402


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value: Any, message: str) -> None:
    if not value:
        raise AssertionError(message)


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


def run_checks() -> dict[str, Any]:
    checks = [
        check_config_roundtrip_and_secret_preserve,
        check_api_routes_roundtrip_dry_run,
        check_webhook_dry_run_signature,
        check_app_bot_send_with_bound_targets,
        check_handoff_dispatch_disabled_and_tenant_targeting,
        check_workflow_business_handoff_routes_to_feishu,
    ]
    results = [check() for check in checks]
    failed = [item for item in results if not item.get("ok")]
    if failed:
        raise AssertionError(failed)
    return {"ok": True, "checks": len(results), "results": results}


def check_config_roundtrip_and_secret_preserve() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="omniauto-feishu-") as temp_dir:
        path = Path(temp_dir) / "feishu_config.json"
        saved = feishu.save_feishu_config(
            {
                "enabled": True,
                "mode": "webhook",
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                "webhook_secret": "secret-one",
                "app_secret": "app-secret-one",
                "default_receive_ids": "ou_a\nou_b",
            },
            path=path,
        )
        preserved = feishu.save_feishu_config(
            {
                "enabled": False,
                "mode": "app_bot",
                "webhook_url": "",
                "webhook_secret": "",
                "app_secret": "",
                "default_receive_ids": ["ou_b", "ou_c"],
            },
            path=path,
        )
        public = feishu.public_feishu_config(preserved)
        ok = (
            saved.get("webhook_secret") == "secret-one"
            and preserved.get("webhook_url") == "https://open.feishu.cn/open-apis/bot/v2/hook/abc"
            and preserved.get("webhook_secret") == "secret-one"
            and preserved.get("app_secret") == "app-secret-one"
            and preserved.get("default_receive_ids") == ["ou_b", "ou_c"]
            and "secret-one" not in json.dumps(public, ensure_ascii=False)
            and public.get("webhook_secret_configured") is True
        )
        return {"name": "config_roundtrip_and_secret_preserve", "ok": ok, "public": public}


def check_api_routes_roundtrip_dry_run() -> dict[str, Any]:
    old_path = feishu.FEISHU_CONFIG_PATH
    with tempfile.TemporaryDirectory(prefix="omniauto-feishu-api-") as temp_dir:
        feishu.FEISHU_CONFIG_PATH = Path(temp_dir) / "feishu_config.json"
        try:
            client = TestClient(create_app())
            saved = client.put(
                "/api/system/feishu-config",
                json={
                    "enabled": True,
                    "mode": "webhook",
                    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                    "webhook_secret": "secret-one",
                },
            )
            fetched = client.get("/api/system/feishu-config")
            tested = client.post("/api/system/feishu-config/test", json={"dry_run": True})
        finally:
            feishu.FEISHU_CONFIG_PATH = old_path
    saved_payload = saved.json()
    fetched_payload = fetched.json()
    tested_payload = tested.json()
    ok = (
        saved.status_code == 200
        and fetched.status_code == 200
        and tested.status_code == 200
        and saved_payload.get("ok") is True
        and fetched_payload.get("webhook_url_configured") is True
        and tested_payload.get("ok") is True
        and tested_payload.get("status") == "dry_run"
        and "secret-one" not in json.dumps(saved_payload, ensure_ascii=False)
        and "secret-one" not in json.dumps(fetched_payload, ensure_ascii=False)
    )
    return {
        "name": "api_routes_roundtrip_dry_run",
        "ok": ok,
        "saved": saved_payload,
        "fetched": fetched_payload,
        "tested": tested_payload,
    }


def check_webhook_dry_run_signature() -> dict[str, Any]:
    config = feishu.normalize_feishu_config(
        {
            "enabled": True,
            "mode": "webhook",
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
            "webhook_secret": "secret-one",
        }
    )
    result = feishu.send_feishu_webhook_text(config, text="hello", dry_run=True)
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    ok = (
        result.get("ok") is True
        and result.get("status") == "dry_run"
        and payload.get("msg_type") == "text"
        and bool(payload.get("timestamp"))
        and bool(payload.get("sign"))
        and payload.get("sign") != "secret-one"
    )
    return {"name": "webhook_dry_run_signature", "ok": ok, "result": result}


def check_app_bot_send_with_bound_targets() -> dict[str, Any]:
    calls: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: int = 0, **kwargs: Any) -> FakeResponse:
        body = json.loads(request.data.decode("utf-8")) if request.data else {}
        calls.append({"url": request.full_url, "headers": dict(request.header_items()), "body": body, "timeout": timeout})
        if str(request.full_url).endswith("/auth/v3/tenant_access_token/internal"):
            return FakeResponse({"code": 0, "msg": "ok", "tenant_access_token": "tenant-token", "expire": 7200})
        return FakeResponse({"code": 0, "msg": "ok", "data": {"message_id": "om_test"}})

    config = feishu.normalize_feishu_config(
        {
            "enabled": True,
            "mode": "app_bot",
            "app_id": "cli_test",
            "app_secret": "app-secret-one",
            "receive_id_type": "open_id",
            "default_receive_ids": ["ou_default"],
            "bound_accounts": [
                {"label": "A", "tenant_id": "tenant_a", "receive_id_type": "open_id", "receive_id": "ou_a"},
                {"label": "B", "tenant_id": "tenant_b", "receive_id_type": "user_id", "receive_id": "user_b"},
            ],
        }
    )
    result = feishu.send_feishu_app_bot_text(config, text="handoff", tenant_id="tenant_b", urlopen_fn=fake_urlopen)
    ok = (
        result.get("ok") is True
        and len(calls) == 2
        and calls[0]["body"]["app_id"] == "cli_test"
        and calls[1]["url"].endswith("/im/v1/messages?receive_id_type=user_id")
        and calls[1]["headers"].get("Authorization") == "Bearer tenant-token"
        and calls[1]["body"]["receive_id"] == "user_b"
        and json.loads(calls[1]["body"]["content"]) == {"text": "handoff"}
    )
    return {"name": "app_bot_send_with_bound_targets", "ok": ok, "result": result, "calls": calls}


def check_handoff_dispatch_disabled_and_tenant_targeting() -> dict[str, Any]:
    case = {
        "tenant_id": "tenant_a",
        "case_id": "handoff_test",
        "target": "文件传输助手",
        "reason": "wechat_logout_detected_by_passive_probe",
        "message_contents": ["微信已掉线，已停机保护。"],
        "payload": {"payload": {"kind": "runtime_transport_risk_handoff"}},
    }
    disabled = feishu.dispatch_handoff_case_to_feishu(case, config={"enabled": False}, dry_run=True)
    targeted = feishu.dispatch_handoff_case_to_feishu(
        case,
        config={
            "enabled": True,
            "mode": "app_bot",
            "app_id": "cli_test",
            "app_secret": "app-secret-one",
            "bound_accounts": [{"label": "A", "tenant_id": "tenant_a", "receive_id": "ou_a"}],
        },
        dry_run=True,
    )
    targets = targeted.get("targets") if isinstance(targeted.get("targets"), list) else []
    ok = (
        disabled.get("status") == "not_configured"
        and targeted.get("ok") is True
        and targeted.get("status") == "dry_run"
        and targeted.get("target_count") == 1
        and targets[0].get("receive_id_type") == "open_id"
    )
    return {"name": "handoff_dispatch_disabled_and_tenant_targeting", "ok": ok, "disabled": disabled, "targeted": targeted}


def check_workflow_business_handoff_routes_to_feishu() -> dict[str, Any]:
    from apps.wechat_ai_customer_service.admin_backend.services import handoff_store  # noqa: E402
    from apps.wechat_ai_customer_service.workflows import listen_and_reply  # noqa: E402

    old_path = handoff_store.HANDOFF_PATH
    calls: list[dict[str, Any]] = []

    def fake_dispatch(case: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(case)
        return {
            "enabled": True,
            "ok": True,
            "status": "dry_run",
            "adapter": "feishu",
            "case_id": case.get("case_id"),
        }

    with tempfile.TemporaryDirectory(prefix="omniauto-feishu-workflow-") as temp_dir:
        handoff_store.HANDOFF_PATH = Path(temp_dir) / "handoff_cases.json"
        try:
            alert = {
                "target": "文件传输助手",
                "reason": "llm_requested_handoff",
                "message_ids": ["msg-business-handoff-1"],
                "message_contents": ["客户要求人工确认合同条款。"],
                "reply_text": "我先帮您记录，稍后请同事核实后给您确认。",
                "product_knowledge": {"needs_handoff": True},
            }
            with patch(
                "apps.wechat_ai_customer_service.admin_backend.services.feishu_integration.dispatch_handoff_case_to_feishu",
                side_effect=fake_dispatch,
            ):
                first = listen_and_reply.create_handoff_case({"handoff": {"case_store_enabled": True}}, alert)
                duplicate = listen_and_reply.create_handoff_case({"handoff": {"case_store_enabled": True}}, alert)
        finally:
            handoff_store.HANDOFF_PATH = old_path

    ok = (
        first.get("ok") is True
        and first.get("dispatch", {}).get("adapter") == "feishu"
        and first.get("dispatch", {}).get("status") == "dry_run"
        and first.get("case_id")
        and len(calls) == 1
        and duplicate.get("deduped") is True
        and duplicate.get("dispatch", {}).get("status") == "deduped_skip"
    )
    return {
        "name": "workflow_business_handoff_routes_to_feishu",
        "ok": ok,
        "first": first,
        "duplicate": duplicate,
        "dispatch_calls": len(calls),
    }


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
