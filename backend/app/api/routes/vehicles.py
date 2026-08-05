from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.auth import require_admin_auth
from app.core.database import get_db
from app.core.request_context import ActorContext, get_actor_context
from app.errors import AppError
from app.schemas.vehicle import VehicleCreate, VehicleImageOrderRequest, VehicleUpdate
from app.services import vehicle_service


router = APIRouter(tags=["vehicles"], dependencies=[Depends(require_admin_auth)])


def _commit(
    db: Session,
    callback,
    *,
    actor: ActorContext,
    operation: str,
    target_id: str | None = None,
):
    try:
        result = callback()
        db.commit()
        db.info.pop("vehicle_files_created", None)
        return result
    except Exception as exc:
        db.rollback()
        for path in db.info.pop("vehicle_files_created", []):
            path.unlink(missing_ok=True)
        vehicle_service.record_vehicle_operation_failure(
            actor,
            operation=operation,
            target_id=target_id,
            error=exc,
        )
        raise


@router.get("/vehicles/excel/template")
def download_excel_template(actor: ActorContext = Depends(get_actor_context)):
    content = vehicle_service.build_excel_template()
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="chejin_vehicle_import_v1.xlsx"'},
    )


@router.post("/vehicles/excel/preview")
def preview_excel(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    data = file.file.read(vehicle_service.get_settings().vehicle_excel_max_bytes + 1)
    return ok(
        _commit(
            db,
            lambda: vehicle_service.preview_excel_import(
                db,
                filename=file.filename or "vehicles.xlsx",
                data=data,
                actor=actor,
            ),
            actor=actor,
            operation="excel_preview",
        )
    )


@router.post("/vehicles/excel/{preview_id}/confirm")
def confirm_excel(
    preview_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: vehicle_service.confirm_excel_import(db, preview_id, actor),
            actor=actor,
            operation="excel_confirm",
            target_id=preview_id,
        )
    )


@router.get("/vehicles/images/{image_id}")
def read_vehicle_image(
    image_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    image, path = vehicle_service.get_vehicle_image(db, image_id)
    return FileResponse(path=path, media_type=image.content_type, filename=Path(image.original_filename).name)


@router.get("/vehicles")
def list_vehicles(
    keyword: str | None = None,
    listing_status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        vehicle_service.list_vehicles(
            db,
            keyword=keyword,
            listing_status=listing_status,
            page=page,
            page_size=page_size,
        )
    )


@router.post("/vehicles")
def create_vehicle(
    payload: VehicleCreate,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(_commit(db, lambda: vehicle_service.create_vehicle(db, payload, actor), actor=actor, operation="create"))


@router.get("/vehicles/{vehicle_id}")
def get_vehicle(
    vehicle_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(vehicle_service.get_vehicle(db, vehicle_id))


@router.put("/vehicles/{vehicle_id}")
def update_vehicle(
    vehicle_id: str,
    payload: VehicleUpdate,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: vehicle_service.update_vehicle(db, vehicle_id, payload, actor),
            actor=actor,
            operation="update",
            target_id=vehicle_id,
        )
    )


@router.post("/vehicles/{vehicle_id}/list")
def list_vehicle(
    vehicle_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: vehicle_service.set_listing(db, vehicle_id, listed=True, actor=actor),
            actor=actor,
            operation="list",
            target_id=vehicle_id,
        )
    )


@router.post("/vehicles/{vehicle_id}/unlist")
def unlist_vehicle(
    vehicle_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: vehicle_service.set_listing(db, vehicle_id, listed=False, actor=actor),
            actor=actor,
            operation="unlist",
            target_id=vehicle_id,
        )
    )


def _buffer_vehicle_images(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    settings = vehicle_service.get_settings()
    if len(files) > settings.vehicle_image_upload_max_files:
        raise AppError(
            "VEHICLE_IMAGE_UPLOAD_FILE_COUNT_EXCEEDED",
            "单次上传图片数量超过限制",
            413,
            {"max_files": settings.vehicle_image_upload_max_files},
        )
    buffered: list[tuple[str, bytes]] = []
    total_bytes = 0
    for item in files:
        data = item.file.read(settings.vehicle_image_max_bytes + 1)
        total_bytes += len(data)
        if total_bytes > settings.vehicle_image_upload_max_total_bytes:
            raise AppError(
                "VEHICLE_IMAGE_UPLOAD_TOTAL_TOO_LARGE",
                "单次上传图片总大小超过限制",
                413,
                {"max_total_bytes": settings.vehicle_image_upload_max_total_bytes},
            )
        buffered.append((item.filename or "image", data))
    return buffered


@router.post("/vehicles/{vehicle_id}/images")
def upload_images(
    vehicle_id: str,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: vehicle_service.upload_vehicle_images(db, vehicle_id, _buffer_vehicle_images(files), actor),
            actor=actor,
            operation="upload_images",
            target_id=vehicle_id,
        )
    )


@router.put("/vehicles/{vehicle_id}/images/order")
def reorder_images(
    vehicle_id: str,
    payload: VehicleImageOrderRequest,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    return ok(
        _commit(
            db,
            lambda: vehicle_service.reorder_vehicle_images(db, vehicle_id, payload.image_ids, actor),
            actor=actor,
            operation="reorder_images",
            target_id=vehicle_id,
        )
    )


@router.delete("/vehicles/{vehicle_id}/images/{image_id}")
def delete_image(
    vehicle_id: str,
    image_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(get_actor_context),
):
    try:
        result, cleanup_id = vehicle_service.prepare_delete_vehicle_image(db, vehicle_id, image_id, actor)
        db.commit()
    except Exception as exc:
        db.rollback()
        vehicle_service.record_vehicle_operation_failure(
            actor,
            operation="delete_image",
            target_id=vehicle_id,
            error=exc,
        )
        raise
    result["file_cleanup_pending"] = not vehicle_service.process_vehicle_file_cleanup(cleanup_id)
    return ok(result)
