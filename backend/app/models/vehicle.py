from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.core.database import Base
from app.models.base import new_id, utcnow


settings = get_settings()
KNOWLEDGE_SCHEMA = None if settings.database_url.startswith("sqlite") else settings.omniauto_knowledge_schema


class KnowledgeTenant(Base):
    __tablename__ = "tenants"
    __table_args__ = {"schema": KNOWLEDGE_SCHEMA} if KNOWLEDGE_SCHEMA else {}

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class KnowledgeCategory(Base):
    __tablename__ = "knowledge_categories"
    __table_args__ = {"schema": KNOWLEDGE_SCHEMA} if KNOWLEDGE_SCHEMA else {}

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    layer: Mapped[str] = mapped_column(String(32), primary_key=True)
    category_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=999)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"
    __table_args__ = (
        Index("idx_knowledge_items_category", "tenant_id", "category_id", "status"),
        Index("idx_knowledge_items_product", "tenant_id", "product_id", "status"),
        {"schema": KNOWLEDGE_SCHEMA},
    ) if KNOWLEDGE_SCHEMA else (
        Index("idx_knowledge_items_category", "tenant_id", "category_id", "status"),
        Index("idx_knowledge_items_product", "tenant_id", "product_id", "status"),
    )

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    layer: Mapped[str] = mapped_column(String(32), primary_key=True)
    category_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(128), primary_key=True, default="")
    item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class VehicleImage(Base):
    __tablename__ = "vehicle_images"
    __table_args__ = (
        UniqueConstraint("tenant_id", "vehicle_id", "sha256", name="uq_vehicle_images_content"),
        Index("idx_vehicle_images_vehicle_order", "tenant_id", "vehicle_id", "sort_order"),
        {"schema": KNOWLEDGE_SCHEMA},
    ) if KNOWLEDGE_SCHEMA else (
        UniqueConstraint("tenant_id", "vehicle_id", "sha256", name="uq_vehicle_images_content"),
        Index("idx_vehicle_images_vehicle_order", "tenant_id", "vehicle_id", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    vehicle_id: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class VehicleFileCleanup(Base):
    __tablename__ = "vehicle_file_cleanups"
    __table_args__ = (
        Index("idx_vehicle_file_cleanups_status_created", "status", "created_at"),
        {"schema": KNOWLEDGE_SCHEMA},
    ) if KNOWLEDGE_SCHEMA else (
        Index("idx_vehicle_file_cleanups_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    vehicle_id: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VehicleImportPreview(Base):
    __tablename__ = "vehicle_import_previews"
    __table_args__ = (
        Index("idx_vehicle_import_previews_tenant_status", "tenant_id", "status", "created_at"),
        {"schema": KNOWLEDGE_SCHEMA},
    ) if KNOWLEDGE_SCHEMA else (
        Index("idx_vehicle_import_previews_tenant_status", "tenant_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    rows_payload: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    result_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
