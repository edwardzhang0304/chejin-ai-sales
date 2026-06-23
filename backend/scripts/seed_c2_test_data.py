import argparse
import json
from uuid import UUID

from sqlalchemy import select, func

from app.core.database import SessionLocal
from app.enums import AssignStatus, ContactType, LeadStatus
from app.models.lead import Lead, LeadContact
from app.models.sales import Sales
from app.models.task import Task
from app.models.worker import Worker
from app.services.contact_utils import normalize_phone
from app.services.worker_token_service import encrypt_worker_token, generate_worker_token, hash_worker_token


DEFAULT_WORKER_ID = "00000000-0000-4000-8000-00000000c201"
DEFAULT_SALES_ID = "00000000-0000-4000-8000-00000000c202"
DEFAULT_LEAD_ID = "00000000-0000-4000-8000-00000000c203"
DEFAULT_CLIENT_INSTANCE_ID = "c2-test-client-001"


def _valid_uuid(value: str) -> str:
    return str(UUID(value))


def _upsert_worker(db, *, worker_id: str, worker_token: str, client_instance_id: str) -> Worker:
    worker = db.get(Worker, worker_id)
    if not worker:
        worker = Worker(id=worker_id, worker_name="C2 测试 Worker", device_name="Windows C2 Test", platform="windows")
        db.add(worker)
    worker.enabled = True
    worker.online_status = "online"
    worker.running_status = "idle"
    worker.run_status = "running"
    worker.rpa_component_status = "ready"
    worker.wechat_status = "logged_in"
    worker.current_task = None
    worker.current_step = "c2_seed_ready"
    worker.local_lock_summary = {"locked": False, "owner": None}
    worker.worker_token_hash = hash_worker_token(worker_token)
    worker.worker_token_encrypted = encrypt_worker_token(worker_token)
    worker.client_binding_state = "bound"
    worker.client_instance_id = client_instance_id
    worker.remark = "C2 专用测试数据：只用于会话绑定和消息监听，不生成 add_friend 任务"
    db.flush()
    return worker


def _upsert_sales(db, *, sales_id: str, worker_id: str) -> Sales:
    sales = db.get(Sales, sales_id)
    if not sales:
        sales = Sales(id=sales_id, sales_name="C2 测试销售")
        db.add(sales)
    sales.phone = "13800002002"
    sales.wechat = "c2_test_sales"
    sales.worker_id = worker_id
    sales.enabled = True
    sales.sort_order = 1
    sales.remark = "C2 专用测试销售"
    db.flush()
    return sales


def _upsert_lead(db, *, lead_id: str, sales_id: str, remark_code: str, phone: str) -> Lead:
    lead = db.get(Lead, lead_id)
    if not lead:
        lead = Lead(
            id=lead_id,
            customer_name="C2 测试客户",
            source_type="c2_test_seed",
            source_name_snapshot="C2测试数据脚本",
            created_by="00000000-0000-0000-0000-000000000000",
        )
        db.add(lead)
    lead.status = LeadStatus.assigned.value
    lead.sales_id = sales_id
    lead.assign_status = AssignStatus.assigned.value
    lead.assign_failure_reason = None
    lead.remark = "C2 专用测试线索：只验证短码会话绑定，不生成 add_friend 任务"
    lead.custom_fields = {**(lead.custom_fields or {}), "remark_code": remark_code, "seed_type": "c2_wechat_binding"}
    lead.updated_by = "00000000-0000-0000-0000-000000000000"
    db.flush()

    existing_phone = db.scalar(
        select(LeadContact).where(
            LeadContact.lead_id == lead.id,
            LeadContact.contact_type == ContactType.phone.value,
            LeadContact.is_primary.is_(True),
        )
    )
    normalized = normalize_phone(phone)
    if not existing_phone:
        existing_phone = LeadContact(lead_id=lead.id, contact_type=ContactType.phone.value, is_primary=True)
        db.add(existing_phone)
    existing_phone.contact_value_encrypted = normalized.encrypted
    existing_phone.contact_value_normalized = normalized.normalized
    existing_phone.contact_hash = normalized.contact_hash
    existing_phone.masked_value = normalized.masked
    db.flush()
    return lead


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed C2-only test data without creating add_friend tasks.")
    parser.add_argument("--remark-code", default="CJTEST01")
    parser.add_argument("--phone", default="13896676680")
    parser.add_argument("--worker-id", default=DEFAULT_WORKER_ID, type=_valid_uuid)
    parser.add_argument("--sales-id", default=DEFAULT_SALES_ID, type=_valid_uuid)
    parser.add_argument("--lead-id", default=DEFAULT_LEAD_ID, type=_valid_uuid)
    parser.add_argument("--client-instance-id", default=DEFAULT_CLIENT_INSTANCE_ID)
    parser.add_argument("--worker-token", default=None)
    args = parser.parse_args()

    worker_token = args.worker_token or generate_worker_token()
    db = SessionLocal()
    try:
        task_count = db.scalar(select(func.count()).select_from(Task)) or 0
        if task_count:
            raise SystemExit(f"tasks 表当前不为空：{task_count}。请使用干净 C2 测试库后再执行，脚本不会自动删除任务。")

        worker = _upsert_worker(db, worker_id=args.worker_id, worker_token=worker_token, client_instance_id=args.client_instance_id)
        sales = _upsert_sales(db, sales_id=args.sales_id, worker_id=worker.id)
        lead = _upsert_lead(db, lead_id=args.lead_id, sales_id=sales.id, remark_code=args.remark_code, phone=args.phone)

        task_count_after = db.scalar(select(func.count()).select_from(Task)) or 0
        if task_count_after:
            db.rollback()
            raise SystemExit(f"安全校验失败：脚本执行后 tasks 表不为空：{task_count_after}")

        db.commit()
        print(
            json.dumps(
                {
                    "ok": True,
                    "message": "C2 test data seeded without add_friend tasks.",
                    "worker": {
                        "id": worker.id,
                        "worker_token": worker_token,
                        "client_instance_id": args.client_instance_id,
                    },
                    "sales": {"id": sales.id, "sales_name": sales.sales_name, "worker_id": sales.worker_id},
                    "lead": {"id": lead.id, "sales_id": lead.sales_id, "remark_code": args.remark_code, "phone": args.phone},
                    "tasks_count": task_count_after,
                    "scan_result_curl": (
                        f"curl -X POST http://127.0.0.1:8000/api/workers/{worker.id}/wechat/sessions/scan-result "
                        f"-H 'Content-Type: application/json' -H 'X-Worker-Token: {worker_token}' "
                        f"-H 'X-Client-Instance-Id: {args.client_instance_id}' "
                        f"-d '{{\"scan_id\":\"scan-c2-001\",\"sidecar_run_id\":\"sidecar-c2-001\","
                        f"\"started_at\":\"2026-06-23T10:00:00+08:00\",\"finished_at\":\"2026-06-23T10:00:02+08:00\","
                        f"\"sessions\":[{{\"rpa_session_key\":\"wx-c2-row-001\",\"display_name\":\"{args.remark_code} 许聪\","
                        f"\"remark_code_candidates\":[\"{args.remark_code}\"],\"row_fingerprint\":\"wx-c2-row-fp-001\","
                        f"\"unread_hint\":true,\"last_message_preview\":\"你好\",\"ocr_confidence\":0.98}}]}}'"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
