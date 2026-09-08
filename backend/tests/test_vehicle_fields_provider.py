"""Acceptance: real HTTP/login -> PostgreSQL -> production Product Master/Evidence/Brain -> HTTP Provider.

Only the external model is controlled. Vehicles are artificial. No mocked catalog,
Evidence, prompt, SQL store or Brain result. Requires an isolated PostgreSQL database.
"""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

from urllib import request, error
from http.cookiejar import CookieJar
import pytest
import uvicorn
from sqlalchemy import select

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models.vehicle import KnowledgeItem
from app.services.auth_service import create_account
from test_vehicles_api import PNG_1X1

RUNTIME = Path(__file__).resolve().parents[2] / "worker-client/omniauto-rpa"
for path in (RUNTIME, RUNTIME / "apps/wechat_ai_customer_service", RUNTIME / "apps/wechat_ai_customer_service/workflows", RUNTIME / "apps/wechat_ai_customer_service/adapters"):
    sys.path.insert(0, str(path))
from apps.wechat_ai_customer_service.workflows.customer_service_brain import maybe_run_customer_service_brain

pytestmark = [pytest.mark.real_auth, pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Requires isolated real PostgreSQL and loopback HTTP")]


class HTTPClient:
    def __init__(self, base):
        self.base = base
        self.opener = request.build_opener(request.HTTPCookieProcessor(CookieJar()))
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def send(self, method, path, json=None, files=None):
        headers = {"Origin": "http://127.0.0.1:5173"}
        body = None
        if json is not None:
            body = __import__("json").dumps(json).encode()
            headers["Content-Type"] = "application/json"
        if files:
            boundary = "VehicleAcceptanceBoundary"
            field, (name, data, mime) = next(iter(files.items()))
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + data + f'\r\n--{boundary}--\r\n'.encode())
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        req = request.Request(self.base + path, data=body, headers=headers, method=method)
        try: response = self.opener.open(req, timeout=20)
        except error.HTTPError as exc: response = exc
        with response:
            text = response.read().decode()
            from types import SimpleNamespace
            return SimpleNamespace(status_code=response.status, text=text, json=lambda: __import__("json").loads(text))
    def get(self, path, **kw): return self.send("GET", path, **kw)
    def post(self, path, **kw): return self.send("POST", path, **kw)
    def put(self, path, **kw): return self.send("PUT", path, **kw)


@pytest.fixture
def http_backend():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        create_account(db, username="vehicle_acceptance", display_name="车辆测试账号", password="Vehicle-Local-Test-2026")
        db.commit()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    with HTTPClient(f"http://127.0.0.1:{port}") as api:
        try:
            for _ in range(100):
                if server.started: break
                time.sleep(.02)
            assert server.started
            login = api.post("/api/auth/login", json={"username": "vehicle_acceptance", "password": "Vehicle-Local-Test-2026"})
            assert login.status_code == 200, login.text
            yield api
        finally:
            server.should_exit = True
            thread.join(5)
            sock.close()


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("case", ["fuel", "hybrid", "range_extended", "electric", "long", "legacy", "unknown", "unlisted"])
def test_saved_vehicle_facts_reach_final_provider_request(http_backend, monkeypatch, tmp_path, fast, case):
    monkeypatch.setenv("WECHAT_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("WECHAT_POSTGRES_DSN", os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://"))
    monkeypatch.setenv("WECHAT_POSTGRES_MIRROR_FILES", "false")
    monkeypatch.setenv("WECHAT_KNOWLEDGE_TENANT", "chejin")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "FAKE-VEHICLE-FIELDS-PROVIDER")
    fields = {"display_name": "验收星河通勤车", "brand": "星河", "model": "通勤款", "public_price": "10.88",
              "series": "德系车", "energy_type": case if case in ("fuel", "hybrid", "range_extended", "electric") else "electric",
              "displacement": "1.5L", "battery_capacity_kwh": "82.500000000000000000000001", "drive_type": "four_wheel_drive",
              "vin": "PRIVATE-VIN-SENTINEL", "internal_notes": "PRIVATE-NOTES-SENTINEL", "purchase_price": "7.66"}
    if case == "long":
        fields.update(brand="品牌" * 50, model="车型" * 100, displacement="排量" * 50, battery_capacity_kwh="9" * 100)
    if case == "unknown":
        for key in ("energy_type", "series", "displacement", "battery_capacity_kwh", "drive_type"): fields.pop(key)
    created = http_backend.post("/api/vehicles", json=fields)
    assert created.status_code == 200, created.text
    code = created.json()["data"]["vehicle_code"]
    assert http_backend.post(f"/api/vehicles/{code}/images", files={"files": ("car.png", PNG_1X1, "image/png")}).status_code == 200
    assert http_backend.post(f"/api/vehicles/{code}/list").status_code == 200
    if case == "legacy":
        with SessionLocal() as db:
            row = db.scalar(select(KnowledgeItem).where(KnowledgeItem.item_id == code))
            payload = deepcopy(row.payload)
            payload["data"]["additional_details"]["series"] = "卡罗拉"
            row.payload = payload
            db.commit()
        fields["series"] = "卡罗拉"
    # An unrelated real update rebuilds the existing projection and preserves all five fields.
    assert http_backend.put(f"/api/vehicles/{code}", json={"location": "测试展厅"}).status_code == 200
    if case == "unlisted": assert http_backend.post(f"/api/vehicles/{code}/unlist").status_code == 200
    after = http_backend.get(f"/api/vehicles/{code}").json()["data"]
    for key in ("energy_type", "series", "displacement", "battery_capacity_kwh", "drive_type"):
        assert after[key] == fields.get(key)
    received = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(payload)
            plan = {"can_answer": True, "answer_mode": "ask_clarifying_question",
                    "reply_segments": ["您想先了解哪项车辆资料？"], "facts_claimed": [],
                    "evidence_used": {"product_ids": [] if case == "unlisted" else [code]},
                    "risk": {"risk_level": "low", "risk_tags": [], "needs_handoff": False},
                    "recommended_action": "send_reply", "confidence": .95}
            body = json.dumps({"choices": [{"message": {"content": json.dumps(plan, ensure_ascii=False)}, "finish_reason": "stop"}]}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {"customer_service_brain": {
        "enabled": True, "mode": "brain_first", "provider": "openai_compatible", "model": "vehicle-normal", "flash_model": "vehicle-fast",
        "base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key": "FAKE-VEHICLE-FIELDS-PROVIDER",
        "routine_product_fast_profile_enabled": fast, "low_authority_fast_profile_enabled": False,
        "min_confidence": .2, "require_evidence": True, "include_evidence_pack_in_audit": True, "include_brain_input_in_audit": True,
        "fallback_to_legacy_on_error": False, "require_final_visible_polish": False,
    }, "llm_reply_synthesis": {"enabled": True, "provider": "openai_compatible", "require_evidence": True},
       "raw_message_store": {"enabled": False}, "final_visible_llm_polish": {"enabled": False}}
    try:
        result = maybe_run_customer_service_brain(config=config, target_name="测试客户", target_state={"conversation_context": {}},
            batch=[{"id": "vehicle-test-message", "sender": "客户", "message_type": "text", "content": "验收星河通勤车的能源类型、车系分类、排量、电池容量和驱动方式是什么？"}],
            combined="验收星河通勤车的能源类型、车系分类、排量、电池容量和驱动方式是什么？", decision={}, reply_text="", intent_assist={}, rag_reply={}, llm_reply={}, product_knowledge={}, data_capture={}, raw_capture={}, customer_profile=None)
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
    evidence_root = Path(os.environ.get("VEHICLE_ACCEPTANCE_EVIDENCE", str(tmp_path)))
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / f"provider-{'fast' if fast else 'normal'}-{case}.json").write_text(json.dumps({"case": case, "artificial_vehicle": True, "database_api_record": after, "provider_requests": received, "brain_result": result}, ensure_ascii=False, indent=2, default=str))
    assert received, result
    user = next(item["content"] for item in received[0]["messages"] if item["role"] == "user")
    prompt, _ = json.JSONDecoder().raw_decode(user.lstrip())
    products = prompt["brain_input"]["content_basis"]["product_master"]["items"]
    if case == "unlisted":
        assert not products
        assert code not in user
        return
    assert received[0]["model"] == ("vehicle-fast" if fast else "vehicle-normal")
    assert result["adoptable"] is True, result.get("reason")
    product = next(item for item in products if item["id"] == code)
    evidence_item = next(item for item in result["evidence_pack"]["knowledge"]["product_master"]["items"] if item["id"] == code)
    assert evidence_item["specs"] == product["specs"]
    assert "specs" in product
    specs = product["specs"]
    if case == "unknown":
        assert all(label not in specs for label in ("能源类型", "车系分类", "排量", "电池包容量", "驱动方式"))
    else:
        expected = {"fuel": "燃油", "hybrid": "新能源混动", "range_extended": "新能源增程", "electric": "新能源纯电"}[fields["energy_type"]]
        assert f"能源类型：{expected}" in specs
        assert f"排量：{fields['displacement']}" in specs
        assert f"电池包容量：{fields['battery_capacity_kwh']} kWh" in specs
        assert "驱动方式：四驱" in specs
        assert ("原车系：卡罗拉" if case == "legacy" else "车系分类：德系车") in specs
        assert "车型：德系车" not in specs
        assert "全时四驱" not in specs and "续航" not in specs
    for sentinel in ("PRIVATE-VIN-SENTINEL", "PRIVATE-NOTES-SENTINEL", "purchase_price", "internal_notes"):
        assert sentinel not in json.dumps(received, ensure_ascii=False)
