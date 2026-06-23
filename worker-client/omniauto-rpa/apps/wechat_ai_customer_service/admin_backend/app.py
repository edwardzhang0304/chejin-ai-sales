"""FastAPI entry point for the local WeChat customer-service knowledge admin."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.auth import compat_router as auth_compat_router
from .api.auth import router as auth_router
from .api.candidates import router as candidates_router
from .api.customers import router as customers_router
from .api.customer_service import router as customer_service_router
from .api.diagnostics import router as diagnostics_router
from .api.drafts import router as drafts_router
from .api.exports import router as exports_router
from .api.generator import router as generator_router
from .api.handoffs import router as handoffs_router
from .api.jobs import router as jobs_router
from .api.knowledge import router as knowledge_router
from .api.learning import router as learning_router
from .api.product_console import router as product_console_router
from .api.rag import router as rag_router
from .api.raw_messages import router as raw_messages_router
from .api.recorder_exports import router as recorder_exports_router
from .api.recorder import router as recorder_router
from .api.system import router as system_router
from .api.sync import router as sync_router
from .api.tenants import router as tenants_router
from .api.uploads import router as uploads_router
from .api.versions import router as versions_router
from .api.workflow import router as workflow_router
from .auth_context import AuthTenantMiddleware


APP_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(
        title="WeChat AI Customer Service Admin",
        version="0.1.0",
        description="Local knowledge admin console for the OmniAuto WeChat customer-service app.",
    )

    app.add_middleware(AuthTenantMiddleware)

    @app.middleware("http")
    async def prevent_admin_static_cache(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

    app.include_router(auth_router)
    app.include_router(auth_compat_router)
    app.include_router(candidates_router)
    app.include_router(customers_router)
    app.include_router(customer_service_router)
    app.include_router(diagnostics_router)
    app.include_router(drafts_router)
    app.include_router(exports_router)
    app.include_router(generator_router)
    app.include_router(handoffs_router)
    app.include_router(jobs_router)
    app.include_router(knowledge_router)
    app.include_router(learning_router)
    app.include_router(product_console_router)
    app.include_router(rag_router)
    app.include_router(raw_messages_router)
    app.include_router(recorder_exports_router)
    app.include_router(recorder_router)
    app.include_router(system_router)
    app.include_router(sync_router)
    app.include_router(tenants_router)
    app.include_router(uploads_router)
    app.include_router(versions_router)
    app.include_router(workflow_router)

    if STATIC_ROOT.exists():
        app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "app": "wechat_ai_customer_service_admin",
            "version": "0.1.0",
            "app_root": str(APP_ROOT),
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("apps.wechat_ai_customer_service.admin_backend.app:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()
