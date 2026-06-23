"""Storage backends for the WeChat AI customer-service app."""

from .config import StorageConfig, load_storage_config, postgres_enabled
from .postgres_store import PostgresJsonStore, get_postgres_store

__all__ = [
    "PostgresJsonStore",
    "StorageConfig",
    "get_postgres_store",
    "load_storage_config",
    "postgres_enabled",
]
