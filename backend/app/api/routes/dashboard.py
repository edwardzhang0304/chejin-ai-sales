from datetime import date
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.api.response import ok
from app.core.auth import require_admin_auth
from app.core.database import get_db
from app.services.dashboard.service import dashboard

router = APIRouter(tags=["dashboard"], dependencies=[Depends(require_admin_auth)])


@router.get("/dashboard")
def stats(start: date | None = None, end: date | None = None,
          vehicle_start: date | None = None, vehicle_end: date | None = None,
          sales_id: str = "all", mode: str = "all", status_sales_id: str = "all",
          db: Session = Depends(get_db)):
    return ok(dashboard(db, start=start, end=end, vehicle_start=vehicle_start, vehicle_end=vehicle_end,
                        sales_id=sales_id, mode=mode, status_sales_id=status_sales_id))
