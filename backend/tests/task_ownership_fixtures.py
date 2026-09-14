"""Synthetic C1 inputs with a real sales owner; never settle tasks for tests."""
from sqlalchemy import select
from app.models.sales import Sales
from app.models.lead import Lead
from app.models.task import Task


def owned_add_friend_task(db, *, worker_id, **fields):
    sales = db.scalar(select(Sales).where(Sales.worker_id == worker_id))
    if sales is None:
        sales = Sales(sales_name='人工测试销售', phone='13900007777', enabled=True, worker_id=worker_id)
        db.add(sales)
        db.flush()
    if fields.get('lead_id'):
        db.get(Lead, fields['lead_id']).sales_id = sales.id
    return Task(sales_id=sales.id, worker_id=worker_id, **fields)
