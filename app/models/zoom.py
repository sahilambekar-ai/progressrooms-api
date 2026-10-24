"""Private Zoom authorization records. Never serialize ORM token attributes."""
import uuid
from datetime import datetime
from sqlalchemy import String, Text, Integer, DateTime, ForeignKey, ForeignKeyConstraint, UniqueConstraint, Index, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base, TimestampMixin


class TeacherZoomConnection(Base, TimestampMixin):
    __tablename__ = "teacher_zoom_connections"
    __table_args__ = (
        UniqueConstraint("organization_id", "member_id", name="uq_zoom_member"),
        UniqueConstraint("organization_id", "id", name="uq_zoom_connection_tenant"),
        ForeignKeyConstraint(["organization_id", "member_id"], ["organization_members.organization_id", "organization_members.id"], name="fk_zoom_member_tenant"),
        Index("uq_zoom_active_identity", "zoom_user_id", unique=True,
              postgresql_where=text("status IN ('ACTIVE', 'SIMULATED')"), sqlite_where=text("status IN ('ACTIVE', 'SIMULATED')")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    zoom_user_id: Mapped[str | None] = mapped_column(String(255))
    zoom_account_id: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(40), default="NOT_CONNECTED", nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    host_key_enc: Mapped[str | None] = mapped_column(Text)
    tokens_enc: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ZoomOAuthAttempt(Base):
    __tablename__ = "zoom_oauth_attempts"
    __table_args__ = (ForeignKeyConstraint(["organization_id", "member_id"], ["organization_members.organization_id", "organization_members.id"], name="fk_zoom_attempt_member_tenant"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    intended_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    connection_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_host_id: Mapped[str | None] = mapped_column(String(255))
    expected_email: Mapped[str | None] = mapped_column(String(255))
    candidate_enc: Mapped[str | None] = mapped_column(Text)


class ZoomWebhookReceipt(Base):
    __tablename__ = "zoom_webhook_receipts"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ZoomRevocation(Base):
    __tablename__ = "zoom_revocations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    zoom_user_id: Mapped[str | None] = mapped_column(String(255))
    token_enc: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
