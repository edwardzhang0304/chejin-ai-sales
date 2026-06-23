import json
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


UNSAFE_SECRET_VALUES = {
    "",
    "change-me-in-production",
    "replace_with_a_random_hash_secret",
    "replace_with_a_random_contact_secret",
    "dev_only_replace_with_random_hash_secret",
    "dev_only_replace_with_random_contact_secret",
    "dev-only-phone-hash-secret-change-before-production",
    "dev-only-contact-encryption-secret-change-before-production",
}
PRODUCTION_ENVIRONMENTS = {"production", "prod"}


class Settings(BaseSettings):
    app_name: str = "Chejin Leads Backend"
    environment: str = "development"
    api_prefix: str = "/api"
    database_url: str = "sqlite:///./chejin_leads.db"
    phone_hash_secret: str = "change-me-in-production"
    contact_encryption_secret: str = "change-me-in-production"
    export_max_rows: int = 1000
    cors_origins: Annotated[list[str], NoDecode] = ["*"]
    auto_create_tables: bool = True
    docs_enabled: bool = True
    auth_enforcement: bool = False
    admin_api_token: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            if value.startswith("["):
                return json.loads(value)
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def validate_production_safety(self):
        if self.environment.lower() in PRODUCTION_ENVIRONMENTS:
            if self.auto_create_tables:
                raise ValueError("生产环境必须设置 AUTO_CREATE_TABLES=false，并使用 Alembic 迁移")
            if self.phone_hash_secret in UNSAFE_SECRET_VALUES:
                raise ValueError("生产环境必须配置安全的 PHONE_HASH_SECRET")
            if self.contact_encryption_secret in UNSAFE_SECRET_VALUES:
                raise ValueError("生产环境必须配置安全的 CONTACT_ENCRYPTION_SECRET")
            if self.auth_enforcement and (not self.admin_api_token or self.admin_api_token in UNSAFE_SECRET_VALUES):
                raise ValueError("生产环境启用鉴权时必须配置安全的 ADMIN_API_TOKEN")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in PRODUCTION_ENVIRONMENTS

    def assert_runtime_safe(self) -> None:
        if self.is_production and self.auto_create_tables:
            raise RuntimeError("生产环境禁止自动建表，请设置 AUTO_CREATE_TABLES=false 并执行 Alembic 迁移")


@lru_cache
def get_settings() -> Settings:
    return Settings()
