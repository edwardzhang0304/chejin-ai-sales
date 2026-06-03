import csv
import io

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.request_context import ActorContext
from app.errors import AppError
from app.models.audit import ExportTask
from app.models.base import utcnow
from app.models.lead import Lead
from app.services.audit_service import write_log
from app.services.lead_service import _lead_list_item


DEFAULT_EXPORT_FIELDS = [
    "customer_name",
    "primary_phone_masked",
    "primary_wechat_masked",
    "status",
    "sales_name",
    "remark_summary",
    "duplicate_count",
    "created_at",
    "updated_at",
]

EXPORT_FIELD_LABELS = {
    "customer_name": "客户名称",
    "primary_phone_masked": "主手机号",
    "primary_wechat_masked": "主微信",
    "status": "状态",
    "sales_name": "销售",
    "remark_summary": "备注摘要",
    "duplicate_count": "重复录入次数",
    "created_at": "创建时间",
    "updated_at": "更新时间",
}


def export_selected_leads(db: Session, lead_ids: list[str], fields: list[str], actor: ActorContext) -> tuple[str, str]:
    if not lead_ids:
        raise AppError("EXPORT_EMPTY_SELECTION", "请选择要导出的线索", 400)
    settings = get_settings()
    if len(lead_ids) > settings.export_max_rows:
        raise AppError("EXPORT_TOO_MANY_ROWS", f"单次最多导出 {settings.export_max_rows} 条线索", 400)

    export_fields = fields or DEFAULT_EXPORT_FIELDS
    unsupported = [field for field in export_fields if field not in EXPORT_FIELD_LABELS]
    if unsupported:
        raise AppError("VALIDATION_ERROR", f"不支持的导出字段：{','.join(unsupported)}", 400)

    leads = list(
        db.scalars(
            select(Lead)
            .options(selectinload(Lead.contacts), selectinload(Lead.sales))
            .where(Lead.id.in_(lead_ids), Lead.deleted_at.is_(None))
            .order_by(Lead.created_at.desc())
        )
    )

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=export_fields)
    writer.writerow({field: EXPORT_FIELD_LABELS[field] for field in export_fields})
    for lead in leads:
        item = _lead_list_item(lead)
        writer.writerow({field: item.get(field) for field in export_fields})

    file_name = f"leads_export_{utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    task = ExportTask(
        export_type="selected_leads",
        status="completed",
        selected_count=len(leads),
        file_name=file_name,
        operator_id=str(actor.operator_id),
        completed_at=utcnow(),
    )
    db.add(task)
    db.flush()
    write_log(
        db,
        actor,
        event_type="leads_exported",
        module="export",
        target_type="export_task",
        target_id=task.id,
        metadata={"selected_count": len(leads), "fields": export_fields, "masked": True},
    )
    return file_name, output.getvalue()
