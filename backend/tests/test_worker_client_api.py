from datetime import timedelta
import threading
import time

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.dialects import postgresql

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models.audit import OperationLog
from app.models.base import utcnow
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.services import task_service
from app.errors import AppError


client = TestClient(app)
HEADERS = {
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Ops Tester",
    "X-Operator-Role": "admin",
}


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _create_worker(name: str = "Windows Worker") -> dict:
    response = client.post(
        "/api/workers",
        json={"worker_name": name, "device_name": "Windows PC", "platform": "windows", "enabled": True},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]


def _bind_worker(worker: dict, client_instance_id: str = "client-a") -> dict:
    response = client.post(
        f"/api/workers/{worker['id']}/client-bind",
        json={"worker_token": worker["worker_token"], "client_instance_id": client_instance_id},
    )
    assert response.status_code == 200
    return response.json()["data"]


def _heartbeat(worker: dict, client_instance_id: str = "client-a", run_status: str = "running", rpa_status: str = "ready"):
    return client.post(
        f"/api/workers/{worker['id']}/heartbeat",
        json={
            "client_instance_id": client_instance_id,
            "run_status": run_status,
            "rpa_component_status": rpa_status,
            "wechat_status": "logged_in",
            "running_status": "idle",
            "current_task": None,
        },
        headers={"X-Worker-Token": worker["worker_token"]},
    )


def _create_sales(worker_id: str) -> str:
    response = client.post(
        "/api/sales",
        json={"sales_name": "张伟", "phone": "13900000001", "enabled": True, "sort_order": 10, "worker_id": worker_id},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]["id"]


def _create_lead(name: str, phone: str) -> dict:
    response = client.post(
        "/api/leads",
        json={"customer_name": name, "phones": [phone], "remark": "预算 10 万"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]


def _first_task() -> dict:
    response = client.get("/api/tasks")
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert items
    return items[0]


def test_task_claim_statement_has_database_row_lock():
    statement = task_service._task_claim_statement("task-lock-contract")
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql


def test_postgres_concurrent_task_claim_allows_only_one_winner():
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock concurrency test")

    worker = _create_worker("并发领取 Worker")
    _bind_worker(worker)
    _heartbeat(worker)
    _create_sales(worker["id"])
    _create_lead("并发领取客户", "13896676681")
    task = _first_task()
    second_started = threading.Event()
    outcomes: list[str] = []

    def second_claim() -> None:
        with SessionLocal() as second_db:
            second_started.set()
            try:
                task_service.claim_task(
                    second_db,
                    task["id"],
                    worker["id"],
                    "second_claim",
                    None,
                    task_service.SYSTEM_TASK_LEASE_ACTOR,
                    client_instance_id="client-a",
                )
                second_db.commit()
                outcomes.append("claimed")
            except AppError as exc:
                second_db.rollback()
                outcomes.append(exc.code)

    with SessionLocal() as first_db:
        first = task_service.claim_task(
            first_db,
            task["id"],
            worker["id"],
            "first_claim",
            None,
            task_service.SYSTEM_TASK_LEASE_ACTOR,
            client_instance_id="client-a",
        )
        assert first["status"] == "running"
        contender = threading.Thread(target=second_claim, daemon=True)
        contender.start()
        assert second_started.wait(timeout=2)
        time.sleep(0.1)
        assert contender.is_alive()
        first_db.commit()

    contender.join(timeout=5)
    assert not contender.is_alive()
    assert outcomes == ["TASK_CLAIM_NOT_ALLOWED"]


def test_worker_client_bind_is_single_instance_and_reset_invalidates_old_client():
    worker = _create_worker()

    bound = _bind_worker(worker, "client-a")
    assert bound["client_binding_state"] == "bound"
    assert bound["client_instance_id"] == "client-a"
    assert bound["run_status"] == "paused"
    assert bound["platform"] == "windows"
    assert "runtime_status" not in bound
    assert "current_task_id" not in bound
    assert "client_bind_status" not in bound

    duplicate = client.post(
        f"/api/workers/{worker['id']}/client-bind",
        json={"worker_token": worker["worker_token"], "client_instance_id": "client-b"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "WORKER_CLIENT_ALREADY_BOUND"

    reset = client.post(f"/api/workers/{worker['id']}/reset-client-bind", json={"force": True}, headers=HEADERS)
    assert reset.status_code == 200
    new_token = reset.json()["data"]["worker_token"]

    old_heartbeat = client.post(
        f"/api/workers/{worker['id']}/heartbeat",
        json={"client_instance_id": "client-a", "run_status": "running"},
        headers={"X-Worker-Token": worker["worker_token"]},
    )
    assert old_heartbeat.status_code == 401

    reset_required = client.post(
        f"/api/workers/{worker['id']}/heartbeat",
        json={"client_instance_id": "client-a", "run_status": "running"},
        headers={"X-Worker-Token": new_token},
    )
    assert reset_required.status_code == 401
    assert reset_required.json()["code"] == "WORKER_CLIENT_BINDING_RESET"


def test_worker_can_pull_claim_report_steps_complete_and_upload_evidence():
    worker = _create_worker()
    _bind_worker(worker)
    _heartbeat(worker)
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    task = _first_task()

    pull = client.get(
        f"/api/workers/{worker['id']}/tasks/pull",
        headers={"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "client-a"},
    )
    assert pull.status_code == 200
    assert pull.json()["data"]["mode"] == "pending"
    assert pull.json()["data"]["task"]["id"] == task["id"]
    assert pull.json()["data"]["task"]["primary_phone"] == "13896676678"
    assert pull.json()["data"]["task"]["primary_phone_masked"] == "138****6678"
    assert pull.json()["data"]["task"]["business_object"]["lead"]["primary_phone"] == "13896676678"
    formal = pull.json()["data"]["task"]
    assert formal["verify_message"].startswith("您好，我是车金二手车的张伟")
    assert formal["remark_code"].startswith("CJ")
    assert not set(formal["remark_code"][2:]) & set("IJLOQ01")
    assert formal["remark_name"] == formal["remark_code"]
    assert formal["remark_code"] in formal["remark_name"]
    assert formal["remark_code_valid"] is True

    task_detail = client.get(f"/api/tasks/{task['id']}", headers=HEADERS)
    assert task_detail.status_code == 200
    assert "primary_phone" not in task_detail.json()["data"]
    assert "primary_phone" not in task_detail.json()["data"]["business_object"]["lead"]
    assert "verify_message" not in task_detail.json()["data"]
    assert "remark_code" not in task_detail.json()["data"]

    claimed = client.post(
        f"/api/tasks/{task['id']}/claim",
        json={"worker_id": worker["id"], "current_step": "checking_rpa"},
        headers={"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "client-a"},
    )
    assert claimed.status_code == 200
    assert claimed.json()["data"]["status"] == "running"
    lease_token = claimed.json()["data"]["lease_fencing_token"]
    assert lease_token > 0
    lease_headers = {
        "X-Worker-Token": worker["worker_token"],
        "X-Client-Instance-Id": "client-a",
        "X-Task-Lease-Fencing-Token": str(lease_token),
    }

    running_pull = client.get(
        f"/api/workers/{worker['id']}/tasks/pull",
        headers={"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "client-a"},
    )
    assert running_pull.json()["data"]["mode"] == "running"
    assert running_pull.json()["data"]["task"]["id"] == task["id"]

    step = client.post(
        f"/api/tasks/{task['id']}/step",
        json={"current_step": "sending_invite", "remark": "正在发送好友申请"},
        headers=lease_headers,
    )
    assert step.status_code == 200

    evidence = client.post(
        f"/api/tasks/{task['id']}/evidences",
        json={
            "evidence_type": "log",
            "content": "RPA send invite clicked",
            "remark": "本机执行日志片段",
            "metadata": {"trace": "worker-local-001"},
        },
        headers={"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "client-a"},
    )
    assert evidence.status_code == 200
    assert evidence.json()["data"]["evidence_type"] == "log"

    completed = client.post(
        f"/api/tasks/{task['id']}/invite-sent",
        json={"remark": "已发送添加通讯录邀请"},
        headers=lease_headers,
    )
    assert completed.status_code == 200
    detail = completed.json()["data"]
    assert detail["status"] == "completed"
    assert detail["result_code"] == "invite_sent"
    assert detail["evidences"][0]["content"] == "RPA send invite clicked"
    assert {"claimed", "step_updated", "completed"}.issubset({event["event_type"] for event in detail["events"]})


def test_worker_claim_requires_online_running_and_rpa_ready_and_rejects_second_running_task():
    worker = _create_worker()
    _bind_worker(worker)
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    first = _first_task()

    paused_claim = client.post(
        f"/api/tasks/{first['id']}/claim",
        json={"worker_id": worker["id"]},
        headers={"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "client-a"},
    )
    assert paused_claim.status_code == 409
    assert paused_claim.json()["code"] == "WORKER_OFFLINE"

    _heartbeat(worker, run_status="running", rpa_status="unavailable")
    unavailable_claim = client.post(
        f"/api/tasks/{first['id']}/claim",
        json={"worker_id": worker["id"]},
        headers={"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "client-a"},
    )
    assert unavailable_claim.status_code == 409
    assert unavailable_claim.json()["code"] == "RPA_COMPONENT_UNAVAILABLE"

    _heartbeat(worker, run_status="running", rpa_status="ready")
    claimed = client.post(
        f"/api/tasks/{first['id']}/claim",
        json={"worker_id": worker["id"]},
        headers={"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "client-a"},
    )
    assert claimed.status_code == 200

    _create_lead("李女士", "13896676679")
    tasks = client.get("/api/tasks?status=pending").json()["data"]["items"]
    second = tasks[0]
    second_claim = client.post(
        f"/api/tasks/{second['id']}/claim",
        json={"worker_id": worker["id"]},
        headers={"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "client-a"},
    )
    assert second_claim.status_code == 409
    assert second_claim.json()["code"] == "WORKER_HAS_RUNNING_TASK"


def test_worker_heartbeat_rejects_legacy_running_status_values():
    worker = _create_worker()
    _bind_worker(worker)
    response = _heartbeat(worker, run_status="running")
    assert response.status_code == 200
    invalid = client.post(
        f"/api/workers/{worker['id']}/heartbeat",
        json={"client_instance_id": "client-a", "run_status": "running", "running_status": "busy"},
        headers={"X-Worker-Token": worker["worker_token"]},
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "WORKER_RUNNING_STATUS_INVALID"


def test_pause_drains_only_exact_registered_flow_and_rejects_new_work():
    worker = _create_worker()
    _bind_worker(worker)
    assert _heartbeat(worker).status_code == 200
    headers = {
        "X-Worker-Token": worker["worker_token"],
        "X-Client-Instance-Id": "client-a",
    }
    conversation_id = "conv-runtime-1"
    with SessionLocal() as db:
        db.add(
            WechatSessionBinding(
                conversation_id=conversation_id,
                worker_id=worker["id"],
                display_name="CJRUNTIME",
                remark_code="CJRUNTIME",
                rpa_session_key="wx:runtime:1",
                row_fingerprint="runtime-row",
                bind_status="bound",
                listen_status="listening",
                allow_listening=True,
            )
        )
        db.commit()
    started = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/start",
        json={
            "flow_id": "read-runtime-1",
            "flow_kind": "c2_read",
            "conversation_id": conversation_id,
            "unread_generation": 0,
        },
        headers=headers,
    )
    assert started.status_code == 200, started.text
    assert started.json()["data"]["status"] == "active"

    paused = client.post(
        f"/api/workers/{worker['id']}/run-status",
        json={"run_status": "paused", "client_instance_id": "client-a"},
        headers=headers,
    )
    assert paused.status_code == 200, paused.text
    state = paused.json()["data"]["inflight_flow_state"]
    assert state["status"] == "draining"
    assert state["flow_id"] == "read-runtime-1"
    assert state["pause_requested_at"]

    forged_new_work = client.get(
        f"/api/workers/{worker['id']}/tasks/pull",
        headers={**headers, "X-Inflight-Flow-Id": "read-runtime-1"},
    )
    assert forged_new_work.status_code == 409
    assert forged_new_work.json()["code"] == "WORKER_NEW_FLOW_NOT_ALLOWED"

    wrong_finish = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/finish",
        json={
            "flow_id": "read-runtime-1",
            "terminal_kind": "failed_before_message_action",
            "conversation_id": conversation_id,
            "error_code": "C2_TARGET_NOT_FOUND",
        },
        headers={**headers, "X-Inflight-Flow-Id": "forged-flow"},
    )
    assert wrong_finish.status_code == 409
    assert wrong_finish.json()["code"] == "WORKER_INFLIGHT_FLOW_MISMATCH"

    finished = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/finish",
        json={
            "flow_id": "read-runtime-1",
            "terminal_kind": "failed_before_message_action",
            "conversation_id": conversation_id,
            "error_code": "C2_TARGET_NOT_FOUND",
        },
        headers={**headers, "X-Inflight-Flow-Id": "read-runtime-1"},
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["data"]["finished"] is True


def test_chat_reply_inflight_flow_requires_conversation_scope():
    worker = _create_worker("C3 Flow 客户范围 Worker")
    _bind_worker(worker)
    assert _heartbeat(worker).status_code == 200
    response = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/start",
        json={
            "flow_id": "chat-reply-without-conversation",
            "flow_kind": "chat_reply",
        },
        headers={
            "X-Worker-Token": worker["worker_token"],
            "X-Client-Instance-Id": "client-a",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
    with SessionLocal() as db:
        current = db.get(Worker, worker["id"])
        assert current.inflight_flow_state == {}


def test_faulted_worker_drains_current_flow_and_cannot_pull_new_work():
    worker = _create_worker("v0.9.32 布局故障 Worker")
    _bind_worker(worker)
    assert _heartbeat(worker).status_code == 200
    headers = {
        "X-Worker-Token": worker["worker_token"],
        "X-Client-Instance-Id": "client-a",
    }
    conversation_id = "conv-pre-send-layout"
    with SessionLocal() as db:
        db.add(
            WechatSessionBinding(
                conversation_id=conversation_id,
                worker_id=worker["id"],
                display_name="CJLAYOUT",
                remark_code="CJLAYOUT",
                rpa_session_key="wx:layout:1",
                row_fingerprint="layout-row",
                bind_status="bound",
                listen_status="listening",
                allow_listening=True,
            )
        )
        db.commit()
    started = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/start",
        json={
            "flow_id": "pre-send-layout-flow",
            "flow_kind": "c2_read",
            "conversation_id": conversation_id,
            "unread_generation": 0,
        },
        headers=headers,
    )
    assert started.status_code == 200

    faulted = client.post(
        f"/api/workers/{worker['id']}/run-status",
        json={"run_status": "faulted", "client_instance_id": "client-a"},
        headers=headers,
    )

    assert faulted.status_code == 200, faulted.text
    data = faulted.json()["data"]
    assert data["run_status"] == "faulted"
    assert data["inflight_flow_state"]["status"] == "draining"
    pulled = client.get(
        f"/api/workers/{worker['id']}/tasks/pull",
        headers=headers,
    )
    assert pulled.status_code == 409
    assert pulled.json()["code"] == "WORKER_NEW_FLOW_NOT_ALLOWED"


def test_c2_inflight_finish_requires_backend_read_completion_proof():
    worker = _create_worker("C2 结算证明 Worker")
    _bind_worker(worker)
    assert _heartbeat(worker).status_code == 200
    headers = {
        "X-Worker-Token": worker["worker_token"],
        "X-Client-Instance-Id": "client-a",
    }
    conversation_id = "11111111-2222-3333-4444-555555555555"
    with SessionLocal() as db:
        db.add(
            WechatSessionBinding(
                conversation_id=conversation_id,
                worker_id=worker["id"],
                display_name="CJPROOF1",
                remark_code="CJPROOF1",
                rpa_session_key="wx:proof:1",
                row_fingerprint="proof-row",
                bind_status="bound",
                listen_status="listening",
                allow_listening=True,
            )
        )
        db.commit()
    flow_id = "read-proof-required"
    assert client.post(
        f"/api/workers/{worker['id']}/inflight-flow/start",
        json={
            "flow_id": flow_id,
            "flow_kind": "c2_read",
            "conversation_id": conversation_id,
            "unread_generation": 0,
        },
        headers=headers,
    ).status_code == 200
    finish_payload = {
        "flow_id": flow_id,
        "terminal_kind": "read_confirmed",
        "conversation_id": conversation_id,
        "error_code": None,
    }
    premature = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/finish",
        json=finish_payload,
        headers={**headers, "X-Inflight-Flow-Id": flow_id},
    )
    assert premature.status_code == 409
    assert premature.json()["code"] == "WORKER_INFLIGHT_FLOW_NOT_SETTLED"
    with SessionLocal() as db:
        binding = db.query(WechatSessionBinding).filter(
            WechatSessionBinding.conversation_id == conversation_id
        ).one()
        binding.last_read_run_id = flow_id
        binding.last_read_completed_at = utcnow()
        binding.last_read_result = "no_change"
        db.commit()
    finished = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/finish",
        json=finish_payload,
        headers={**headers, "X-Inflight-Flow-Id": flow_id},
    )
    assert finished.status_code == 200, finished.text


def test_c2_inflight_read_failed_no_fact_is_an_audited_terminal():
    worker = _create_worker("C2 无事实失败 Worker")
    _bind_worker(worker)
    assert _heartbeat(worker).status_code == 200
    headers = {
        "X-Worker-Token": worker["worker_token"],
        "X-Client-Instance-Id": "client-a",
    }
    conversation_id = "11111111-2222-3333-4444-666666666666"
    with SessionLocal() as db:
        db.add(
            WechatSessionBinding(
                conversation_id=conversation_id,
                worker_id=worker["id"],
                display_name="CJNOFACT",
                remark_code="CJNOFACT",
                rpa_session_key="wx:no-fact:1",
                row_fingerprint="no-fact-row",
                bind_status="bound",
                listen_status="listening",
                allow_listening=True,
            )
        )
        db.commit()
    flow_id = "read-sidecar-no-fact"
    assert client.post(
        f"/api/workers/{worker['id']}/inflight-flow/start",
        json={
            "flow_id": flow_id,
            "flow_kind": "c2_read",
            "conversation_id": conversation_id,
            "unread_generation": 0,
        },
        headers=headers,
    ).status_code == 200

    finished = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/finish",
        json={
            "flow_id": flow_id,
            "terminal_kind": "read_failed_no_fact",
            "conversation_id": conversation_id,
            "error_code": "C2_MESSAGE_OCR_FAILED",
        },
        headers={**headers, "X-Inflight-Flow-Id": flow_id},
    )

    assert finished.status_code == 200, finished.text
    with SessionLocal() as db:
        audit = db.query(OperationLog).filter(
            OperationLog.event_type == "worker_inflight_read_failed_no_fact"
        ).one()
        assert audit.extra_metadata["flow_id"] == flow_id
        assert audit.extra_metadata["error_code"] == "C2_MESSAGE_OCR_FAILED"

    conflicting_flow_id = "read-sidecar-formed-fact"
    assert client.post(
        f"/api/workers/{worker['id']}/inflight-flow/start",
        json={
            "flow_id": conflicting_flow_id,
            "flow_kind": "c2_read",
            "conversation_id": conversation_id,
            "unread_generation": 0,
        },
        headers=headers,
    ).status_code == 200
    with SessionLocal() as db:
        db.add(
            MessageEvent(
                conversation_id=conversation_id,
                worker_id=worker["id"],
                rpa_session_key="wx:formed-fact",
                read_run_id=conflicting_flow_id,
                contract_version=3,
                source_message_key="source-formed-fact",
                dedupe_key="dedupe-formed-fact",
                sender_role="customer",
                message_type="text",
                content="已形成事实",
                raw_payload={},
                evidence={},
            )
        )
        db.commit()
    rejected = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/finish",
        json={
            "flow_id": conflicting_flow_id,
            "terminal_kind": "read_failed_no_fact",
            "conversation_id": conversation_id,
            "error_code": "C2_MESSAGE_OCR_FAILED",
        },
        headers={
            **headers,
            "X-Inflight-Flow-Id": conflicting_flow_id,
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "WORKER_INFLIGHT_FLOW_NOT_SETTLED"


def test_postgres_inflight_start_pause_finish_are_serialized():
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL inflight row-lock concurrency test")

    worker = _create_worker("在途流程并发 Worker")
    _bind_worker(worker)
    assert _heartbeat(worker).status_code == 200
    headers = {
        "X-Worker-Token": worker["worker_token"],
        "X-Client-Instance-Id": "client-a",
    }
    conversation_id = "conv-concurrent-flow"
    with SessionLocal() as db:
        db.add(
            WechatSessionBinding(
                conversation_id=conversation_id,
                worker_id=worker["id"],
                display_name="CJCONCUR",
                remark_code="CJCONCUR",
                rpa_session_key="wx:concurrent:flow",
                row_fingerprint="concurrent-flow-row",
                bind_status="bound",
                listen_status="listening",
                allow_listening=True,
            )
        )
        db.commit()

    def c2_start_payload(flow_id: str) -> dict:
        return {
            "flow_id": flow_id,
            "flow_kind": "c2_read",
            "conversation_id": conversation_id,
            "unread_generation": 0,
        }

    def concurrent_posts(requests: list[tuple[str, dict, dict]]) -> list[tuple[int, str]]:
        barrier = threading.Barrier(len(requests))
        outcomes: list[tuple[int, str]] = []

        def run(path: str, payload: dict, request_headers: dict) -> None:
            barrier.wait(timeout=5)
            response = client.post(path, json=payload, headers=request_headers)
            outcomes.append(
                (response.status_code, str(response.json().get("code") or ""))
            )

        threads = [
            threading.Thread(target=run, args=item, daemon=True)
            for item in requests
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        return outcomes

    start_path = f"/api/workers/{worker['id']}/inflight-flow/start"
    finish_path = f"/api/workers/{worker['id']}/inflight-flow/finish"
    pause_path = f"/api/workers/{worker['id']}/run-status"

    start_start = concurrent_posts(
        [
            (start_path, c2_start_payload("read-concurrent-a"), headers),
            (start_path, c2_start_payload("read-concurrent-b"), headers),
        ]
    )
    assert sorted(code for code, _ in start_start) == [200, 409]
    profile = client.get(f"/api/workers/{worker['id']}", headers=HEADERS).json()["data"]
    winner = profile["inflight_flow_state"]["flow_id"]
    finish_headers = {**headers, "X-Inflight-Flow-Id": winner}
    assert client.post(
        finish_path,
        json={
            "flow_id": winner,
            "terminal_kind": "failed_before_message_action",
            "conversation_id": conversation_id,
            "error_code": "C2_TARGET_NOT_FOUND",
        },
        headers=finish_headers,
    ).status_code == 200

    start_pause = concurrent_posts(
        [
            (start_path, c2_start_payload("read-concurrent-pause"), headers),
            (pause_path, {"run_status": "paused", "client_instance_id": "client-a"}, headers),
        ]
    )
    assert all(code in {200, 409} for code, _ in start_pause)
    profile = client.get(f"/api/workers/{worker['id']}", headers=HEADERS).json()["data"]
    assert profile["run_status"] == "paused"
    state = profile["inflight_flow_state"]
    if state:
        assert state["flow_id"] == "read-concurrent-pause"
        assert state["status"] == "draining", (start_pause, profile)
        assert client.post(
            finish_path,
            json={
                "flow_id": "read-concurrent-pause",
                "terminal_kind": "failed_before_message_action",
                "conversation_id": conversation_id,
                "error_code": "C2_TARGET_NOT_FOUND",
            },
            headers={**headers, "X-Inflight-Flow-Id": "read-concurrent-pause"},
        ).status_code == 200

    assert client.post(
        pause_path,
        json={"run_status": "running", "client_instance_id": "client-a"},
        headers=headers,
    ).status_code == 200
    assert client.post(
        start_path,
        json=c2_start_payload("read-concurrent-finish"),
        headers=headers,
    ).status_code == 200
    pause_finish = concurrent_posts(
        [
            (pause_path, {"run_status": "paused", "client_instance_id": "client-a"}, headers),
            (
                finish_path,
                {
                    "flow_id": "read-concurrent-finish",
                    "terminal_kind": "failed_before_message_action",
                    "conversation_id": conversation_id,
                    "error_code": "C2_TARGET_NOT_FOUND",
                },
                {**headers, "X-Inflight-Flow-Id": "read-concurrent-finish"},
            ),
        ]
    )
    assert sorted(code for code, _ in pause_finish) == [200, 200]
    profile = client.get(f"/api/workers/{worker['id']}", headers=HEADERS).json()["data"]
    assert profile["run_status"] == "paused"
    assert profile["inflight_flow_state"] == {}


def test_paused_draining_task_renews_only_with_exact_flow_header():
    worker = _create_worker()
    _bind_worker(worker)
    assert _heartbeat(worker).status_code == 200
    _create_sales(worker["id"])
    _create_lead("暂停续租客户", "13896676672")
    task = _first_task()
    headers = {
        "X-Worker-Token": worker["worker_token"],
        "X-Client-Instance-Id": "client-a",
    }
    started = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/start",
        json={"flow_id": task["id"], "flow_kind": "task"},
        headers=headers,
    )
    assert started.status_code == 200, started.text
    claimed = client.post(
        f"/api/tasks/{task['id']}/claim",
        json={"worker_id": worker["id"], "current_step": "claimed"},
        headers={**headers, "X-Inflight-Flow-Id": task["id"]},
    )
    assert claimed.status_code == 200, claimed.text
    fencing = claimed.json()["data"]["lease_fencing_token"]
    paused = client.post(
        f"/api/workers/{worker['id']}/run-status",
        json={"run_status": "paused", "client_instance_id": "client-a"},
        headers=headers,
    )
    assert paused.status_code == 200, paused.text

    forged = client.post(
        f"/api/tasks/{task['id']}/lease/renew",
        json={"lease_fencing_token": fencing},
        headers={**headers, "X-Inflight-Flow-Id": "other-flow"},
    )
    assert forged.status_code == 409
    assert forged.json()["code"] == "WORKER_INFLIGHT_FLOW_MISMATCH"

    renewed = client.post(
        f"/api/tasks/{task['id']}/lease/renew",
        json={"lease_fencing_token": fencing},
        headers={**headers, "X-Inflight-Flow-Id": task["id"]},
    )
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["data"]["status"] == "running"

    premature_finish = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/finish",
        json={
            "flow_id": task["id"],
            "terminal_kind": "task_terminal",
            "conversation_id": None,
            "error_code": None,
        },
        headers={**headers, "X-Inflight-Flow-Id": task["id"]},
    )
    assert premature_finish.status_code == 409
    assert premature_finish.json()["code"] == (
        "WORKER_INFLIGHT_FLOW_NOT_SETTLED"
    )


def test_pause_after_exact_task_registration_still_allows_original_claim():
    worker = _create_worker()
    _bind_worker(worker)
    assert _heartbeat(worker).status_code == 200
    _create_sales(worker["id"])
    _create_lead("暂停登记窗口客户", "13896676673")
    task = _first_task()
    headers = {
        "X-Worker-Token": worker["worker_token"],
        "X-Client-Instance-Id": "client-a",
    }
    started = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/start",
        json={"flow_id": task["id"], "flow_kind": "task"},
        headers=headers,
    )
    assert started.status_code == 200, started.text
    paused = client.post(
        f"/api/workers/{worker['id']}/run-status",
        json={"run_status": "paused", "client_instance_id": "client-a"},
        headers=headers,
    )
    assert paused.status_code == 200, paused.text

    forged = client.post(
        f"/api/tasks/{task['id']}/claim",
        json={"worker_id": worker["id"], "current_step": "claimed"},
        headers={**headers, "X-Inflight-Flow-Id": "forged-flow"},
    )
    assert forged.status_code == 409
    assert forged.json()["code"] == "WORKER_INFLIGHT_FLOW_MISMATCH"

    claimed = client.post(
        f"/api/tasks/{task['id']}/claim",
        json={"worker_id": worker["id"], "current_step": "claimed"},
        headers={**headers, "X-Inflight-Flow-Id": task["id"]},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["id"] == task["id"]
    assert claimed.json()["data"]["status"] == "running"


def test_task_lease_renewal_rejects_stale_fencing_and_expiry_is_terminal():
    worker = _create_worker()
    _bind_worker(worker)
    _heartbeat(worker)
    _create_sales(worker["id"])
    _create_lead("租约测试客户", "13896676680")
    task = _first_task()
    worker_headers = {
        "X-Worker-Token": worker["worker_token"],
        "X-Client-Instance-Id": "client-a",
    }

    claimed = client.post(
        f"/api/tasks/{task['id']}/claim",
        json={"worker_id": worker["id"], "current_step": "checking_rpa"},
        headers=worker_headers,
    )
    assert claimed.status_code == 200
    token = claimed.json()["data"]["lease_fencing_token"]
    original_expiry = claimed.json()["data"]["lease_expires_at"]

    renewed = client.post(
        f"/api/tasks/{task['id']}/lease/renew",
        json={"lease_fencing_token": token, "current_step": "phone_search_started"},
        headers=worker_headers,
    )
    assert renewed.status_code == 200
    assert renewed.json()["data"]["lease_fencing_token"] == token
    assert renewed.json()["data"]["lease_expires_at"] >= original_expiry

    stale = client.post(
        f"/api/tasks/{task['id']}/lease/renew",
        json={"lease_fencing_token": token + 1, "current_step": "must_not_run"},
        headers=worker_headers,
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "TASK_LEASE_FENCING_STALE"

    with SessionLocal() as db:
        row = db.get(Task, task["id"])
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

    expired_pull = client.get(
        f"/api/workers/{worker['id']}/tasks/pull",
        headers=worker_headers,
    )
    assert expired_pull.status_code == 200
    assert expired_pull.json()["data"]["mode"] == "lease_expired"
    assert expired_pull.json()["data"]["reason"] == "TASK_LEASE_EXPIRED"
    assert expired_pull.json()["data"]["task"] is None

    detail = client.get(f"/api/tasks/{task['id']}", headers=HEADERS)
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "failed"
    assert detail.json()["data"]["error_code"] == "TASK_LEASE_EXPIRED"
