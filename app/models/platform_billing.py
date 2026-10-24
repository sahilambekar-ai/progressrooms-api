"""Append-only qualifying joins and frozen monthly platform statements."""
import uuid
from datetime import date, datetime
from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base, TimestampMixin


class StudentJoinEvent(Base):
    __tablename__ = "student_join_events"
    __table_args__ = (Index("ix_student_join_studio_month_user", "organization_id", "month", "user_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    enrollment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("session_enrollments.id"), nullable=False)
    occurrence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("class_occurrences.id"), nullable=False)
    month: Mapped[date] = mapped_column(Date, nullable=False)
    clicked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pricing_region: Mapped[str | None] = mapped_column(String(20), nullable=True)


class StudentJoinTeacher(Base):
    __tablename__ = "student_join_teachers"
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("student_join_events.id"), primary_key=True)
    teacher_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organization_members.id"), primary_key=True, index=True)


class PlatformInvoice(Base, TimestampMixin):
    __tablename__ = "platform_invoices"
    __table_args__ = (UniqueConstraint("organization_id", "month", name="uq_platform_invoice_studio_month"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    month: Mapped[date] = mapped_column(Date, nullable=False)
    active_students: Mapped[int] = mapped_column(Integer, nullable=False)
    free_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    rate: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
