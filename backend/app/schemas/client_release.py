from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ClientReleaseLatestResponse(BaseModel):
    update_available: bool
    latest_version: str
    channel: str
    platform: str
    artifact_url: str | None = None
    artifact_size_bytes: int | None = None
    artifact_sha256: str | None = None
    manifest_signature: str | None = None
    signature_key_id: str | None = None
    git_commit: str | None = None
    package_manifest_sha256: str | None = None
    published_at: datetime | None = None
    release_notes: str = ""
    minimum_updater_version: str
    rollback_safe: bool
    client_ahead_of_channel: bool = False
