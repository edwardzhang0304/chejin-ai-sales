"""Shared response models for the local knowledge admin API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    ok: bool
    app: str
    version: str
    app_root: str


class Issue(BaseModel):
    severity: str = Field(pattern="^(info|warning|error)$")
    title: str
    detail: str = ""
    target: str = ""


class ApplyResult(BaseModel):
    ok: bool
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

