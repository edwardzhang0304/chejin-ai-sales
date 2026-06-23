from contextvars import ContextVar
from uuid import uuid4


_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    return str(uuid4())


def get_request_id() -> str:
    request_id = _request_id_var.get()
    if request_id:
        return request_id
    return new_request_id()


def set_request_id(request_id: str):
    return _request_id_var.set(request_id)


def reset_request_id(token) -> None:
    _request_id_var.reset(token)
