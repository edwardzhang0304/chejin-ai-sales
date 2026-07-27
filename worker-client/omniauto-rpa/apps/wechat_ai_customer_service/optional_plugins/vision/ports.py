"""Neutral host ports for the independently portable vision capability.

The protocols deliberately contain no Brain, scheduler, ledger, voice, image
provider, or product-master implementation.  A host may bind its existing
window/RPA infrastructure without teaching the core runtime about that host.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol


class RpaLeasePort(Protocol):
    def lease(self, action: str, *, timeout_seconds: float) -> AbstractContextManager[Any]: ...


class ConversationTargetPort(Protocol):
    def confirm_target(self, context: dict[str, Any]) -> dict[str, Any]: ...


class WindowFramePort(Protocol):
    def capture_frame(self, context: dict[str, Any]) -> dict[str, Any]: ...


class UiActionPort(Protocol):
    def right_click(self, x: int, y: int) -> None: ...

    def click(self, x: int, y: int) -> None: ...


class ClipboardPort(Protocol):
    def sequence_number(self) -> int | None: ...

    def read_current_bitmap(self) -> Any: ...


class VisionProviderPort(Protocol):
    def understand(self, request: dict[str, Any]) -> dict[str, Any]: ...


class ProductImageRepositoryPort(Protocol):
    def get_product(self, product_id: str) -> dict[str, Any] | None: ...

    def list_products(self) -> list[dict[str, Any]]: ...

    def save_product(self, record: dict[str, Any]) -> dict[str, Any]: ...


class VisionAuditPort(Protocol):
    def record(self, event: str, payload: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class VisionHostPorts:
    """Optional host bindings used by the public service API.

    ``connector`` is retained as a compatibility binding for the current
    WeChat host.  New third-party hosts can supply the neutral fine-grained
    ports without importing the bundled connector.
    """

    connector: Any = None
    rpa_lease: RpaLeasePort | None = None
    conversation_target: ConversationTargetPort | None = None
    window_frame: WindowFramePort | None = None
    ui_action: UiActionPort | None = None
    clipboard: ClipboardPort | None = None
    vision_provider: VisionProviderPort | None = None
    product_images: ProductImageRepositoryPort | None = None
    audit: VisionAuditPort | None = None
