from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.request_context import ActorContext
from app.errors import AppError
from app.models.lead import Lead
from app.models.sales import Sales
from app.schemas.sales import SalesUpsert
from app.services.audit_service import write_log


def _mask_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 11:
        return f"{digits[:3]}****{digits[-4:]}"
    return value


def list_sales(db: Session) -> list[dict]:
    rows = db.execute(
        select(Sales, func.count(Lead.id))
        .outerjoin(Lead, (Lead.sales_id == Sales.id) & (Lead.deleted_at.is_(None)))
        .where(Sales.deleted_at.is_(None))
        .group_by(Sales.id)
        .order_by(Sales.sort_order.is_(None), Sales.sort_order.asc(), Sales.created_at.desc())
    ).all()
    return [
        {
            "id": sales.id,
            "sales_name": sales.sales_name,
            "phone": _mask_phone(sales.phone),
            "wechat": sales.wechat,
            "feishu_user_id": sales.feishu_user_id,
            "enabled": sales.enabled,
            "sort_order": sales.sort_order,
            "remark": sales.remark,
            "lead_count": lead_count,
        }
        for sales, lead_count in rows
    ]


def create_sales(db: Session, payload: SalesUpsert, actor: ActorContext) -> Sales:
    sales = Sales(**payload.model_dump())
    db.add(sales)
    db.flush()
    write_log(
        db,
        actor,
        event_type="sales_created",
        module="sales",
        target_type="sales",
        target_id=sales.id,
        after_data={"sales_name": sales.sales_name, "enabled": sales.enabled},
    )
    return sales


def update_sales(db: Session, sales_id: str, payload: SalesUpsert, actor: ActorContext) -> Sales:
    sales = db.get(Sales, sales_id)
    if not sales or sales.deleted_at:
        raise AppError("SALES_NOT_FOUND", "销售不存在", 404)

    before = {
        "sales_name": sales.sales_name,
        "enabled": sales.enabled,
        "sort_order": sales.sort_order,
    }
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(sales, key, value)

    after = {
        "sales_name": sales.sales_name,
        "enabled": sales.enabled,
        "sort_order": sales.sort_order,
    }
    event_type = "sales_updated"
    if before["enabled"] != after["enabled"]:
        event_type = "sales_enabled_changed"

    write_log(
        db,
        actor,
        event_type=event_type,
        module="sales",
        target_type="sales",
        target_id=sales.id,
        before_data=before,
        after_data=after,
    )
    return sales
