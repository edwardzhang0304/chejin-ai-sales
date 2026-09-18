"""A fact correction cannot carry ordinary ingest or sending commands."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

Rect = tuple[StrictInt, StrictInt, StrictInt, StrictInt]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CorrectionAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_id: str = Field(min_length=1, max_length=255)
    observed_text: str = Field(max_length=4000)
    rect: Rect


class CorrectionOCRItem(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0.9, le=1)
    box: list[tuple[float, float]] = Field(min_length=4, max_length=4)
    left: float
    top: float
    right: float
    bottom: float
    center_x: float
    center_y: float


class HistoricalCorrectionProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provenance: Literal["capture_digest", "legacy_correlated"]
    original_path: str = Field(min_length=1, max_length=2048)
    sidecar_run_id: str = Field(min_length=1, max_length=255)
    image_sha256: Digest
    dimensions: tuple[StrictInt, StrictInt]
    digest_recorded_at: datetime
    viewport: Rect
    original_observations: list[dict] = Field(min_length=3, max_length=200)
    original_rect: Rect
    bubble_rect: Rect
    crop_rect: Rect
    padding: Literal[8]
    scale: Literal[2]
    resample: Literal["lanczos"]
    anchors: list[CorrectionAnchor] = Field(min_length=2, max_length=200)
    ocr_method: Literal["rapidocr_complete_bubble_v1"]
    ocr_items: list[CorrectionOCRItem] = Field(min_length=1, max_length=100)


class MessageTextCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["historical_text_correction"]
    version: Literal[1]
    conversation_id: str = Field(min_length=1, max_length=36)
    binding_id: str = Field(min_length=1, max_length=36)
    authorization_revision: str = Field(min_length=1, max_length=128)
    message_event_id: str = Field(min_length=1, max_length=36)
    source_message_key: str = Field(min_length=1, max_length=255)
    original_read_run_id: str = Field(min_length=1, max_length=128)
    original_observation_id: str = Field(min_length=1, max_length=255)
    original_text_sha256: Digest
    expected_effective_version: int = Field(strict=True, ge=0)
    corrected_text: str = Field(min_length=1, max_length=4000)
    proof: HistoricalCorrectionProof
    proof_sha256: Digest
    image_base64: str = Field(min_length=1, max_length=5592408)

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("correction_version_must_be_integer")
        return value
