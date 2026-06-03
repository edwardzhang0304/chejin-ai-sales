from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.core.request_id import get_request_id


def ok(data: Any = None, message: str = "success", trace_id: str | None = None) -> dict[str, Any]:
    return {"code": "OK", "message": message, "data": data, "trace_id": trace_id or get_request_id()}


def error_response(status_code: int, code: str, message: str, data: Any = None, trace_id: str | None = None) -> JSONResponse:
    content = jsonable_encoder({"code": code, "message": message, "data": data or {}, "trace_id": trace_id or get_request_id()})
    return JSONResponse(
        status_code=status_code,
        content=content,
    )
