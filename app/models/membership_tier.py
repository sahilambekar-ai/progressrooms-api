import uuid
from sqlalchemy import String, Boolean, Text, Integer, Numeric, ForeignKey, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base, TimestampMixin, SoftDeleteMixin

class MembershipTier(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "membership_tiers"
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_membership_tiers_price_nonnegative"),
        CheckConstraint("offer_price IS NULL OR (offer_price >= 0 AND offer_price <= price)",
                        name="ck_membership_tiers_offer_price"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    tier_type: Mapped[str] = mapped_column(String(50), default="MONTHLY", nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    offer_price: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)
    validity_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    total_classes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    organization: Mapped["Organization"] = relationship()
    classes: Mapped[list["MembershipTierClass"]] = relationship(back_populates="tier", cascade="all, delete-orphan")

class MembershipTierClass(Base, TimestampMixin):
    __tablename__ = "membership_tier_classes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("membership_tiers.id", ondelete="CASCADE"), index=True, nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False)

    tier: Mapped["MembershipTier"] = relationship(back_populates="classes")
    session: Mapped["Session"] = relationship()
