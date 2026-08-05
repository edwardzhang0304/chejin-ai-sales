"""Run a disposable HTTP -> Product Master -> KnowledgeRuntime smoke check."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from http.cookiejar import CookieJar
from uuid import uuid4

import psycopg


BASE_URL = os.getenv("VEHICLE_SMOKE_BASE_URL", "http://127.0.0.1:8000")
TENANT_ID = os.getenv("WECHAT_KNOWLEDGE_TENANT", "chejin")
SCHEMA = os.getenv("WECHAT_POSTGRES_SCHEMA", "wechat_ai_customer_service")
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c020000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082"
)
ORIGIN = os.getenv("VEHICLE_SMOKE_ORIGIN", "http://127.0.0.1:5173")
OPENER = build_opener(HTTPCookieProcessor(CookieJar()))


def request(path: str, *, method: str = "GET", body: bytes | None = None, content_type: str | None = None) -> dict:
    headers = {"Origin": ORIGIN} if method not in {"GET", "HEAD"} else {}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        with OPENER.open(Request(BASE_URL + path, data=body, headers=headers, method=method), timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc.code} {exc.read().decode('utf-8', 'replace')}") from exc


def json_request(path: str, payload: dict, *, method: str) -> dict:
    return request(
        path,
        method=method,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
    )


def multipart_image() -> tuple[bytes, str]:
    boundary = f"----chejin-{uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="smoke.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode() + PNG_1X1 + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def cleanup(vehicle_id: str) -> None:
    dsn = os.getenv("WECHAT_POSTGRES_DSN")
    if not dsn or not vehicle_id:
        return
    storage_keys: list[str] = []
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT storage_key FROM {SCHEMA}.vehicle_images WHERE tenant_id=%s AND vehicle_id=%s",
                (TENANT_ID, vehicle_id),
            )
            storage_keys = [str(row[0]) for row in cursor.fetchall()]
            cursor.execute(
                f"DELETE FROM {SCHEMA}.vehicle_images WHERE tenant_id=%s AND vehicle_id=%s",
                (TENANT_ID, vehicle_id),
            )
            cursor.execute(
                f"DELETE FROM {SCHEMA}.knowledge_items WHERE tenant_id=%s AND layer='product_master' AND category_id='products' AND item_id=%s",
                (TENANT_ID, vehicle_id),
            )
            cursor.execute("DELETE FROM operation_logs WHERE target_id=%s", (vehicle_id,))
    root = Path(os.getenv("VEHICLE_IMAGE_STORAGE_ROOT", "/app/data/vehicle-images")).resolve()
    for key in storage_keys:
        path = (root / key).resolve()
        if root in path.parents:
            path.unlink(missing_ok=True)


def main() -> int:
    vehicle_id = ""
    try:
        username = str(os.getenv("VEHICLE_SMOKE_ADMIN_USERNAME") or "").strip()
        password = str(os.getenv("VEHICLE_SMOKE_ADMIN_PASSWORD") or "")
        if not username or not password:
            raise RuntimeError("VEHICLE_SMOKE_ADMIN_USERNAME and VEHICLE_SMOKE_ADMIN_PASSWORD are required")
        json_request(
            "/api/auth/login",
            {"username": username, "password": password},
            method="POST",
        )
        created = json_request(
            "/api/vehicles",
            {
                "display_name": "Product Master 冒烟测试车",
                "brand": "车金测试",
                "series": "KnowledgeRuntime",
                "model": "Smoke-1",
                "customer_description": "仅用于自动化验证，执行后删除。",
            },
            method="POST",
        )["data"]
        vehicle_id = str(created["vehicle_code"])
        json_request(f"/api/vehicles/{vehicle_id}", {"public_price": 8.88}, method="PUT")
        body, content_type = multipart_image()
        uploaded = request(
            f"/api/vehicles/{vehicle_id}/images",
            method="POST",
            body=body,
            content_type=content_type,
        )["data"]
        if uploaded["succeeded"] != 1:
            raise AssertionError(uploaded)
        request(f"/api/vehicles/{vehicle_id}/list", method="POST")

        root = Path(os.getenv("C3_OMNIAUTO_ROOT", "/app/omniauto-rpa"))
        for path in (
            root,
            root / "apps" / "wechat_ai_customer_service",
            root / "apps" / "wechat_ai_customer_service" / "workflows",
            root / "apps" / "wechat_ai_customer_service" / "adapters",
        ):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        from apps.wechat_ai_customer_service.workflows.knowledge_runtime import KnowledgeRuntime

        products = KnowledgeRuntime(tenant_id=TENANT_ID).list_items("products")
        matched = next((item for item in products if item.get("id") == vehicle_id), None)
        if not matched:
            raise AssertionError("listed vehicle was not visible to OmniAuto KnowledgeRuntime")
        if (matched.get("data") or {}).get("name") != "Product Master 冒烟测试车":
            raise AssertionError("Product Master payload mismatch")
        if "internal" in (matched.get("data") or {}):
            raise AssertionError("internal vehicle fields leaked into Product Master data")
        evidence = {}
        if os.getenv("RUN_REAL_BRAIN") == "1":
            from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
            from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import build_reply_evidence_pack

            adapter = RealOmniAutoAIEngineAdapter()
            config = adapter._load_config()
            batch = [
                {
                    "id": "vehicle-product-smoke-message",
                    "sender_role": "customer",
                    "message_type": "text",
                    "content": "Product Master 冒烟测试车现在多少钱？",
                }
            ]
            evidence_pack = build_reply_evidence_pack(
                config=config,
                target_name="CJSMOKE",
                target_state={},
                batch=batch,
                combined="Product Master 冒烟测试车现在多少钱？",
                decision={},
                reply_text="",
                intent_assist={},
                rag_reply={},
                llm_reply={},
                product_knowledge={},
                data_capture={},
                raw_capture={"messages": batch},
                customer_profile={},
            )
            evidence_ids = list(evidence_pack.get("evidence_ids") or [])
            if not any(vehicle_id in str(item) for item in evidence_ids):
                raise AssertionError(f"Brain Evidence Pack did not contain smoke vehicle: {evidence_ids}")
            decision = adapter.generate_reply_decision(
                conversation_context={
                    "conversation_id": "vehicle-product-smoke",
                    "remark_code": "CJSMOKE",
                    "history": [],
                },
                message_batch={
                    "id": "vehicle-product-smoke",
                    "trigger_type": "customer_message",
                    "messages": batch,
                },
            )
            brain_result = (decision.raw_payload or {}).get("omniauto_brain_result") or {}
            audit = brain_result.get("audit_summary") or {}
            plan = brain_result.get("brain_plan") or {}
            used = plan.get("evidence_used") or {}
            product_ids = list(used.get("product_ids") or [])
            if int(audit.get("structured_evidence_count") or 0) < 1:
                raise AssertionError(f"real Brain did not receive structured evidence: {audit}")
            evidence = {
                "decision": decision.decision,
                "error_code": decision.error_code,
                "model": (brain_result.get("llm_status") or {}).get("model"),
                "provider": (brain_result.get("llm_status") or {}).get("provider"),
                "duration_seconds": brain_result.get("duration_seconds"),
                "structured_evidence_count": audit.get("structured_evidence_count"),
                "evidence_pack_vehicle_hit": True,
                "product_ids": product_ids,
            }
        print(
            json.dumps(
                {
                    "ok": True,
                    "vehicle_id": vehicle_id,
                    "knowledge_runtime_visible": True,
                    "real_brain": evidence or None,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        cleanup(vehicle_id)


if __name__ == "__main__":
    raise SystemExit(main())
