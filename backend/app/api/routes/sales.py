from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.database import get_db
from app.core.request_context import ActorContext, get_actor_context
from app.schemas.sales import SalesUpsert
from app.services import sales_service


router = APIRouter(tags=["sales"])


@router.get("/sales")
def list_sales(db: Session = Depends(get_db)):
    return ok({"items": sales_service.list_sales(db)})


@router.post("/sales")
def create_sales(
    payload: SalesUpsert,
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


@router.put("/sales/{sales_id}")
def update_sales(
    sales_id: str,
    payload: SalesUpsert,
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

