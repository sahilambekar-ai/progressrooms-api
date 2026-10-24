"""Platform subscription purchases are independent from student class orders."""
import uuid
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, ForeignKey, UniqueConstraint, CheckConstraint, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base, TimestampMixin


class SaaSCheckout(Base, TimestampMixin):
    __tablename__ = "saas_checkouts"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_saas_checkout_retry"),
        CheckConstraint("total_paise > 0", name="ck_saas_positive_total"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), index=True)
    purchaser_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plans.id"))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="CREATING", index=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    total_paise: Mapped[int] = mapped_column(Integer)
    quote: Mapped[dict] = mapped_column(JSON)
    provider_key_id: Mapped[str] = mapped_column(String(255))
    provider_order_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    service_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    service_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SaaSWebhookEvent(Base, TimestampMixin):
    __tablename__ = "saas_webhook_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(255), unique=True)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
