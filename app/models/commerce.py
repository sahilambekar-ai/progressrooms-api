import uuid
from datetime import datetime
from sqlalchemy import String, Boolean, Text, Integer, Numeric, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base, TimestampMixin
from app.common.enums import OrderStatus, PaymentStatus, EnrollmentStatus

class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    order_number: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    total_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=OrderStatus.PENDING.value, index=True, nullable=False)

    provider_order_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    offering_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    offering_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    credit_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validity_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped["User"] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    enrollment: Mapped["SessionEnrollment"] = relationship(back_populates="order", uselist=False, foreign_keys="SessionEnrollment.order_id")

class OrderItem(Base, TimestampMixin):
    __tablename__ = "order_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id"), index=True, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    subtotal: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="items")
    session: Mapped["Session"] = relationship()

class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)
    payment_provider: Mapped[str] = mapped_column(String(50), default="RAZORPAY", nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    provider_signature: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=PaymentStatus.PENDING.value, index=True, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)

    order: Mapped["Order"] = relationship(back_populates="payments")

class PaymentWebhookEvent(Base, TimestampMixin):
    __tablename__ = "payment_webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class OrganizationPaymentAccount(Base, TimestampMixin):
    __tablename__ = "organization_payment_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), default="RAZORPAY", nullable=False)
    key_id: Mapped[str] = mapped_column(String(255), nullable=False)
    key_secret_enc: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

class SessionEnrollment(Base, TimestampMixin):
    __tablename__ = "session_enrollments"
    __table_args__ = (
        Index("ix_session_enrollments_org_user", "organization_id", "user_id"),
        Index("ix_session_enrollments_session_status", "session_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), unique=True, nullable=True)
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=EnrollmentStatus.ACTIVE.value, index=True, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sessions_attended: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    billing_value: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    billing_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    billing_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    billing_label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user: Mapped["User"] = relationship(back_populates="enrollments")
    session: Mapped["Session"] = relationship(back_populates="enrollments")
    order: Mapped["Order"] = relationship(back_populates="enrollment", foreign_keys=[order_id])
    attendance_records: Mapped[list["AttendanceRecord"]] = relationship(back_populates="enrollment")
