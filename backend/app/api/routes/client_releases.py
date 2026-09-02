from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.response import ok
from app.core.database import get_db
from app.schemas.client_release import ClientReleaseLatestResponse
from app.errors import AppError
from app.services.client_release_service import (
    latest_client_release,
    record_client_release_query,
    resolve_client_release_download,
)


router = APIRouter(tags=["client-releases"])


@router.get("/client-releases/latest")
def get_latest_client_release(
    request: Request,
    current_version: str = Query(min_length=5, max_length=32),
    platform: str = Query(min_length=3, max_length=32),
    channel: str = Query(min_length=2, max_length=16),
    x_client_instance_id: str | None = Header(default=None, alias="X-Client-Instance-Id"),
    db: Session = Depends(get_db),
):
    # Deliberately unauthenticated: a fresh, unbound installation must be able
    # to discover an update.  The instance id is rate-limit identity only and
    # never grants trust to the returned artifact.
    try:
        record_client_release_query(
            db,
            ip_address=request.client.host if request.client else "unknown",
            client_instance_id=x_client_instance_id,
        )
        payload = ClientReleaseLatestResponse.model_validate(
            latest_client_release(
                db,
                current_version=current_version,
                platform=platform,
                channel=channel,
                client_instance_id=x_client_instance_id,
                requester_ip=request.client.host if request.client else "unknown",
            )
        )
        db.commit()
    except AppError:
        # Persist the bounded counter even when this exact request is rejected.
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise
    return ok(payload.model_dump(mode="json"))


@router.get("/client-releases/artifacts/{lease_id}")
def download_client_release_artifact(
    lease_id: str,
    token: str = Query(min_length=32, max_length=256),
    db: Session = Depends(get_db),
):
    release, path = resolve_client_release_download(
        db,
        lease_id=lease_id,
        raw_token=token,
    )
    return FileResponse(
        path,
        media_type="application/zip",
        filename=(
            f"CheJinWorkerClient-v{release.version}-{release.channel}-"
            f"{release.platform}.zip"
        ),
    )
