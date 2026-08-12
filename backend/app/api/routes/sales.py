from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.auth import require_admin_auth
from app.core.database import get_db
from app.core.request_context import ActorContext, get_actor_context
from app.schemas.sales import SalesCreate, SalesUpdate, SalesWorkerBindRequest
from app.services import sales_service


router = APIRouter(tags=["sales"], dependencies=[Depends(require_admin_auth)])


@router.get("/sales")
def list_sales(db: Session = Depends(get_db)):
    return ok({"items": sales_service.list_sales(db)})


@router.post("/sales")
def create_sales(
    payload: SalesCreate,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        sales = sales_service.create_sales(db, payload, actor)
        db.commit()
        return ok({"id": sales.id})
    except Exception:
        db.rollback()
        raise


@router.get("/sales/{sales_id}")
def get_sales(sales_id: str, db: Session = Depends(get_db)):
    return ok(sales_service.get_sales_detail(db, sales_id))


@router.put("/sales/{sales_id}")
def update_sales(
    sales_id: str,
    payload: SalesUpdate,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        sales = sales_service.update_sales(db, sales_id, payload, actor)
        db.commit()
        return ok({"id": sales.id})
    except Exception:
        db.rollback()
        raise


@router.post("/sales/{sales_id}/worker-binding")
def bind_sales_worker(
    sales_id: str,
    payload: SalesWorkerBindRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        data = sales_service.bind_worker(db, sales_id, payload, actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise


@router.delete("/sales/{sales_id}/worker-binding")
def clear_sales_worker(
    sales_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        data = sales_service.bind_worker(db, sales_id, SalesWorkerBindRequest(worker_id=None), actor)
        db.commit()
        return ok(data)
    except Exception:
        db.rollback()
        raise
