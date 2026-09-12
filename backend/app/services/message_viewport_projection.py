"""Load the same pure OCR projection used by Worker/Sidecar."""
from typing import Any

from app.contracts.shared_rules import shared_adapter


def normalized_projection_text(value: Any) -> str:
    return shared_adapter("message_viewport_projection").normalized_projection_text(value)
