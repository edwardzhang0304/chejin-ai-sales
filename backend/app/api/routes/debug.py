from fastapi import APIRouter


router = APIRouter(tags=["debug"])


@router.get("/_debug/raise-internal-error")
def raise_internal_error():
    raise RuntimeError("debug internal error for integration testing")
