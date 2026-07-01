import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.response import error_response
from app.api.routes import assignment, c3, debug, leads, operation_logs, sales, tasks, wechat, workers
from app.core.config import get_settings
from app.core.database import Base, SessionLocal, engine
from app.core.request_id import new_request_id, reset_request_id, set_request_id
from app.errors import AppError
from app import models  # noqa: F401


settings = get_settings()
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    docs_url = "/docs" if settings.docs_enabled else None
    openapi_url = "/openapi.json" if settings.docs_enabled else None
    app = FastAPI(title=settings.app_name, docs_url=docs_url, redoc_url=None, openapi_url=openapi_url)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
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

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        trace_id = getattr(request.state, "request_id", None)
        return error_response(exc.status_code, exc.code, exc.message, exc.data, trace_id=trace_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        trace_id = getattr(request.state, "request_id", None)
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
        return {"status": "ok", "database": "ok"}

    app.include_router(leads.router, prefix=settings.api_prefix)
    app.include_router(sales.router, prefix=settings.api_prefix)
    app.include_router(workers.router, prefix=settings.api_prefix)
    app.include_router(wechat.router, prefix=settings.api_prefix)
    app.include_router(c3.router, prefix=settings.api_prefix)
    app.include_router(tasks.router, prefix=settings.api_prefix)
    app.include_router(operation_logs.router, prefix=settings.api_prefix)
    app.include_router(assignment.router, prefix=settings.api_prefix)
    if not settings.is_production:
        app.include_router(debug.router, prefix=settings.api_prefix)
    return app


app = create_app()
