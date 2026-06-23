"""Storage configuration helpers."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


VALID_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


@dataclass(frozen=True)
class StorageConfig:
    backend: str
    postgres_dsn: str
    postgres_schema: str
    mirror_files: bool

    @property
    def use_postgres(self) -> bool:
        return self.backend == "postgres"

    @property
    def postgres_configured(self) -> bool:
        return bool(self.postgres_dsn.strip())


def load_storage_config() -> StorageConfig:
    backend = (os.getenv("WECHAT_STORAGE_BACKEND") or "json").strip().lower()
    if backend not in {"json", "postgres"}:
        backend = "json"
    schema = (os.getenv("WECHAT_POSTGRES_SCHEMA") or "wechat_ai_customer_service").strip()
    validate_schema_name(schema)
    mirror = parse_bool(os.getenv("WECHAT_POSTGRES_MIRROR_FILES"), default=backend == "postgres")
    return StorageConfig(
        backend=backend,
        postgres_dsn=(os.getenv("WECHAT_POSTGRES_DSN") or os.getenv("DATABASE_URL") or "").strip(),
        postgres_schema=schema,
        mirror_files=mirror,
    )


def postgres_enabled() -> bool:
    config = load_storage_config()
    return config.use_postgres and config.postgres_configured


def validate_schema_name(schema: str) -> None:
    if not VALID_SCHEMA_RE.fullmatch(schema):
        raise ValueError("WECHAT_POSTGRES_SCHEMA must be a safe PostgreSQL identifier")


def parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
