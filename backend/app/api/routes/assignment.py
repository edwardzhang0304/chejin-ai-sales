from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.database import get_db
from app.services.assignment_service import round_robin_state


router = APIRouter(tags=["assignment"])


@router.get("/assignment/round-robin-state")
def get_round_robin_state(db: Session = Depends(get_db)):
    return ok(round_robin_state(db))

