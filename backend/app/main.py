import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.response import error_response
from app.api.routes import assignment, auth_session, c3, debug, leads, observability, operation_logs, sales, tasks, vehicles, wechat, workers
from app.core.config import get_settings
from app.core.database import Base, SessionLocal, engine
from app.core.request_id import new_request_id, reset_request_id, set_request_id
from app.contracts.message_limits import C2_MESSAGE_INGEST_MAX_BYTES
from app.errors import AppError
from app import models  # noqa: F401
from app.services.ai_adapter import check_ai_engine_readiness
from app.services.c3_recovery import C3BatchRecoveryLoop
from app.services.feishu_adapter import check_feishu_readiness
from app.services.feishu_service import recover_handoff_notifications
from app.services.observability_service import (
    abandon_open_server_stages_after_restart,
)
from app.services.vehicle_service import knowledge_runtime_readiness, retry_pending_vehicle_file_cleanups


settings = get_settings()
logger = logging.getLogger(__name__)


class MessageIngestBodyTooLarge(Exception):
    pass


def _recover_observability_on_startup_best_effort() -> int:
    """Observability recovery must never prevent the business API from starting."""

    try:
        with SessionLocal() as observability_db:
            abandoned_stages = abandon_open_server_stages_after_restart(
                observability_db
            )
            observability_db.commit()
        return abandoned_stages
    except Exception:
        logger.warning(
            "backend observability startup recovery ignored",
            exc_info=True,
        )
        return 0


class C2IngestBodyLimitMiddleware:
    """Reject an oversized ingest body while ASGI is still streaming it."""

    def __init__(self, app: Callable[..., Awaitable[None]], *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or not str(scope.get("path") or "").endswith(
            "/wechat/messages/ingest"
        ):
            await self.app(scope, receive, send)
            return

        headers = {
            key.lower(): value
            for key, value in scope.get("headers") or []
        }
        raw_length = headers.get(b"content-length", b"")
        try:
            content_length = int(raw_length or b"0")
        except ValueError:
            content_length = 0
        if content_length > self.max_bytes:
            await error_response(
                413,
                "MESSAGE_INGEST_REQUEST_TOO_LARGE",
                "消息入库请求超过大小限制",
                {"max_bytes": self.max_bytes},
            )(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > self.max_bytes:
                    raise MessageIngestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except MessageIngestBodyTooLarge:
            await error_response(
                413,
                "MESSAGE_INGEST_REQUEST_TOO_LARGE",
                "消息入库请求超过大小限制",
                {"max_bytes": self.max_bytes},
            )(scope, receive, send)


def create_app() -> FastAPI:
    docs_url = "/docs" if settings.docs_enabled else None
    openapi_url = "/openapi.json" if settings.docs_enabled else None
    app = FastAPI(title=settings.app_name, docs_url=docs_url, redoc_url=None, openapi_url=openapi_url)
    app.add_middleware(
        C2IngestBodyLimitMiddleware,
        max_bytes=C2_MESSAGE_INGEST_MAX_BYTES,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or new_request_id()
        request.state.request_id = request_id
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            reset_request_id(token)

    @app.on_event("startup")
    def create_tables_for_local_dev() -> None:
        settings.assert_runtime_safe()
        if settings.auto_create_tables:
            Base.metadata.create_all(bind=engine)
        abandoned_stages = _recover_observability_on_startup_best_effort()
        if abandoned_stages:
            logger.info(
                "abandoned pre-restart backend observability stages=%s",
                abandoned_stages,
            )
        cleanup = retry_pending_vehicle_file_cleanups()
        if cleanup["pending"]:
            logger.warning("vehicle file cleanup remains pending count=%s", cleanup["pending"])
        feishu_recovery = recover_handoff_notifications()
        if feishu_recovery["unknown_settled"] or feishu_recovery["pending_attempted"]:
            logger.info(
                "Feishu handoff recovery unknown_settled=%s pending_attempted=%s",
                feishu_recovery["unknown_settled"],
                feishu_recovery["pending_attempted"],
            )
        recovery = C3BatchRecoveryLoop()
        recovery.start()
        app.state.c3_batch_recovery = recovery

    @app.on_event("shutdown")
    def stop_c3_batch_recovery() -> None:
        recovery = getattr(app.state, "c3_batch_recovery", None)
        if recovery is not None:
            recovery.stop()

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        trace_id = getattr(request.state, "request_id", None)
        return error_response(exc.status_code, exc.code, exc.message, exc.data, trace_id=trace_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        trace_id = getattr(request.state, "request_id", None)
        if any(
            error.get("type") == "sales_feishu_id_server_managed"
            for error in exc.errors()
        ):
            return error_response(
                422,
                "SALES_FEISHU_ID_SERVER_MANAGED",
                "飞书用户标识只能由服务端维护",
                trace_id=trace_id,
            )
        return error_response(400, "VALIDATION_ERROR", "参数错误", {"errors": exc.errors()}, trace_id=trace_id)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        trace_id = getattr(request.state, "request_id", None)
        logger.exception("Unhandled backend exception trace_id=%s path=%s", trace_id, request.url.path, exc_info=exc)
        return error_response(500, "INTERNAL_SERVER_ERROR", "服务内部错误，请联系管理员并提供 trace_id", trace_id=trace_id)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        with SessionLocal() as db:
            db.execute(text("select 1"))
            knowledge = knowledge_runtime_readiness(db)
        brain = check_ai_engine_readiness()
        feishu = check_feishu_readiness()
        return {
            "status": "ok",
            "database": "ok",
            "knowledge": knowledge,
            "brain": brain,
            "feishu": feishu,
        }

    app.include_router(leads.router, prefix=settings.api_prefix)
    app.include_router(auth_session.router, prefix=settings.api_prefix)
    app.include_router(sales.router, prefix=settings.api_prefix)
    app.include_router(workers.router, prefix=settings.api_prefix)
    app.include_router(wechat.router, prefix=settings.api_prefix)
    app.include_router(c3.router, prefix=settings.api_prefix)
    app.include_router(tasks.router, prefix=settings.api_prefix)
    app.include_router(operation_logs.router, prefix=settings.api_prefix)
    app.include_router(observability.router, prefix=settings.api_prefix)
    app.include_router(assignment.router, prefix=settings.api_prefix)
    app.include_router(vehicles.router, prefix=settings.api_prefix)
    if not settings.is_production:
        app.include_router(debug.router, prefix=settings.api_prefix)
    return app


app = create_app()
