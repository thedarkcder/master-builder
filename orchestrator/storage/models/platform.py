from __future__ import annotations

# Model modules share SQLAlchemy symbols from the storage model base.
# ruff: noqa: F401
from .base import (
    Base,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    Mapped,
    String,
    Text,
    UniqueConstraint,
    VectorJSONCompat,
    datetime,
    mapped_column,
    text,
    uuid4,
)


class AdminNotification(Base):
    __tablename__ = "admin_notifications"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_admin_notifications_fingerprint"),
        Index("ix_admin_notifications_status", "status"),
        Index("ix_admin_notifications_scope", "scope_type", "scope_id"),
        Index("ix_admin_notifications_tenant_status", "tenant_id", "status"),
        Index("ix_admin_notifications_kind_status", "kind", "status"),
    )

    notification_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    scope_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    action_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    context_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    first_emitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_emitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ManagedSecret(Base):
    __tablename__ = "managed_secrets"

    secret_ref: Mapped[str] = mapped_column(String(255), primary_key=True)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    setting_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
