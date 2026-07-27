from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.contracts.c2 import recovery_action_for_error
from app.core.request_id import get_request_id


def ok(data: Any = None, message: str = "success", trace_id: str | None = None) -> dict[str, Any]:
    return {"code": "OK", "message": message, "data": data, "trace_id": trace_id or get_request_id()}


def error_retryable(status_code: int) -> bool:
    return int(status_code) in {408, 425, 429, 500, 502, 503, 504}


def error_response(status_code: int, code: str, message: str, data: Any = None, trace_id: str | None = None) -> JSONResponse:
    details = dict(data) if isinstance(data, dict) else {}
    details["retryable"] = error_retryable(status_code)
    recovery_action = recovery_action_for_error(code, status_code)
    details["recovery_action"] = recovery_action
    # A payload terminal is safe only after the owning route/service has
    # persisted its technical terminal and explicitly supplied confirmation.
    # Target termination is itself confirmed by the backend binding check.
    if recovery_action == "target_terminated":
        details["terminal_confirmed"] = True
    content = jsonable_encoder({"code": code, "message": message, "data": details, "trace_id": trace_id or get_request_id()})
    return JSONResponse(
        status_code=status_code,
        content=content,
    )
