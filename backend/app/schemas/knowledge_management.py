from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class KnowledgePublishPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["create", "update", "archive"]
    item_id: str
    expected_updated_at: datetime | None = None


class KnowledgeDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=5000)
    expected_updated_at: datetime | None = None


class KnowledgePublishConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str
    content_digest: str = Field(min_length=64, max_length=64)


class KnowledgeRollbackPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_release_id: str
