import uuid
from datetime import datetime
from sqlalchemy import String, Boolean, Text, Numeric, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base, TimestampMixin

class Plan(Base, TimestampMixin):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Numeric(10, 2), default=0.00, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)
    billing_interval: Mapped[str] = mapped_column(String(20), default="MONTHLY", nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE", nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    features: Mapped[list["PlanFeature"]] = relationship(back_populates="plan", cascade="all, delete-orphan")
    subscriptions: Mapped[list["OrganizationSubscription"]] = relationship(back_populates="plan")

class PlanFeature(Base, TimestampMixin):
    __tablename__ = "plan_features"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), index=True, nullable=False)
    feature_key: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    feature_value: Mapped[str] = mapped_column(String(255), nullable=False)

    plan: Mapped["Plan"] = relationship(back_populates="features")

class OrganizationSubscription(Base, TimestampMixin):
    __tablename__ = "organization_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), unique=True, nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plans.id"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="TRIAL", nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="subscription")
    plan: Mapped["Plan"] = relationship(back_populates="subscriptions")
