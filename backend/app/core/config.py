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
    c3_ai_adapter_mode: str = "real"
    c3_reply_action_ttl_seconds: int = 300
    c3_omniauto_root: str = "/app/omniauto-rpa"
    c3_omniauto_config_path: str | None = None
    c3_omniauto_provider: str | None = None
    c3_omniauto_model: str | None = None
    c3_omniauto_base_url: str | None = None
    c3_brain_provider_timeout_seconds: float = 180.0
    c3_batch_stale_after_seconds: float = 240.0
    c3_batch_recovery_max_attempts: int = 2
    c3_batch_retry_delay_seconds: float = 5.0
    c3_batch_recovery_poll_seconds: float = 10.0
    c3_send_ack_stale_after_seconds: float = 300.0
    c3_recall_after_hours: int = 72
    c3_recall_max_cycles: int = 3
    c3_recall_daily_limit: int = 1
    c3_recall_quiet_start_hour: int = 21
    c3_recall_quiet_end_hour: int = 9
    task_lease_seconds: int = 90
    c2_friend_acceptance_visible_ttl_seconds: float = 90.0

    # The same runtime env file also carries provider-owned variables such as
    # OPENAI_API_KEY. Chejin reads only its own settings while OmniAuto
    # resolves provider variables directly from the process environment.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

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
        if self.c3_batch_stale_after_seconds <= self.c3_brain_provider_timeout_seconds:
            raise ValueError("C3 批次失效时间必须大于 Brain 提供商调用超时")
        if self.c3_batch_recovery_max_attempts < 1:
            raise ValueError("C3 批次恢复次数必须至少为 1")
        if self.c3_batch_retry_delay_seconds < 0:
            raise ValueError("C3 批次重试等待时间不能小于 0")
        if self.c3_batch_recovery_poll_seconds < 0:
            raise ValueError("C3 批次恢复轮询时间不能小于 0")
        if self.c3_send_ack_stale_after_seconds <= 0:
            raise ValueError("C3 发送回执失效时间必须大于 0")
        if self.task_lease_seconds < 30:
            raise ValueError("服务端任务租约不能短于 30 秒")
        if self.c2_friend_acceptance_visible_ttl_seconds <= 0:
            raise ValueError("好友通过首屏可见证据有效期必须大于 0")
        if self.environment.lower() in PRODUCTION_ENVIRONMENTS:
            if self.auto_create_tables:
                raise ValueError("生产环境必须设置 AUTO_CREATE_TABLES=false，并使用 Alembic 迁移")
            if self.phone_hash_secret in UNSAFE_SECRET_VALUES:
                raise ValueError("生产环境必须配置安全的 PHONE_HASH_SECRET")
            if self.contact_encryption_secret in UNSAFE_SECRET_VALUES:
                raise ValueError("生产环境必须配置安全的 CONTACT_ENCRYPTION_SECRET")
            if self.auth_enforcement and (not self.admin_api_token or self.admin_api_token in UNSAFE_SECRET_VALUES):
                raise ValueError("生产环境启用鉴权时必须配置安全的 ADMIN_API_TOKEN")
            if self.c3_ai_adapter_mode != "real":
                raise ValueError("生产环境必须设置 C3_AI_ADAPTER_MODE=real")
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
