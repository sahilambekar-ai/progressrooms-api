import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base, TimestampMixin

class PlatformCommunicationSetting(Base, TimestampMixin):
    """
    Singleton platform-wide communication switches.
    Controls operational dispatch across all studios.
    User authentication emails (OTP, password reset) bypass these switches.
    """
    __tablename__ = "platform_communication_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True
    )

    updated_by_user: Mapped["User"] = relationship("User", foreign_keys=[updated_by_user_id])


class CommunicationChannelAuditLog(Base):
    """
    Records every change to platform communication channel switches,
    capturing who made the change, previous state, new state, and when.
    """
    __tablename__ = "communication_channel_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel: Mapped[str] = mapped_column(String(50), nullable=False)  # "WHATSAPP", "EMAIL", "ALL"
    previous_state: Mapped[bool] = mapped_column(Boolean, nullable=False)
    new_state: Mapped[bool] = mapped_column(Boolean, nullable=False)
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True
    )
    changed_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    changed_by_user: Mapped["User"] = relationship("User", foreign_keys=[changed_by_user_id])


class CommunicationLog(Base):
    """
    Comprehensive record of operational dispatches across WhatsApp & Email channels.
    Includes status (SENT, FAILED, SUPPRESSED) and reasons (e.g. "Suppressed: email disabled").
    """
    __tablename__ = "communication_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True
    )
    recipient_email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    recipient_phone: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # "WHATSAPP_ADMIN", "WHATSAPP_CLASSES", "EMAIL"
    message_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)  # CLASS_REMINDER_2H, CLASS_REMINDER_15M, INVOICE_ISSUED, TEST_MESSAGE, etc.
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # "SENT", "FAILED", "SUPPRESSED"
    suppression_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)  # e.g. "Suppressed: email disabled"
    subject_or_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True
    )

    recipient_user: Mapped["User"] = relationship("User", foreign_keys=[recipient_user_id])
    organization: Mapped["Organization"] = relationship("Organization", foreign_keys=[organization_id])


class UserCommunicationPreference(Base, TimestampMixin):
    """
    Tracks recipient consent and opt-out preferences across WhatsApp & Email channels.
    Outbound operational notifications respect opt-out status.
    """
    __tablename__ = "user_communication_preferences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False
    )
    whatsapp_opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User")


class ClassReminderSchedule(Base, TimestampMixin):
    """
    Maintains scheduled reminder state and version tracking per class occurrence.
    Supports schedule version checks, 2-hour preparation notifications, 15-minute alerts,
    and automatic suppression of superseded reminder jobs.
    """
    __tablename__ = "class_reminder_schedules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    class_occurrence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("class_occurrences.id", ondelete="CASCADE"),
        index=True,
        nullable=False
    )
    schedule_version: Mapped[int] = mapped_column(default=1, nullable=False)
    reminder_type: Mapped[str] = mapped_column(String(50), nullable=False)  # "REMINDER_2H", "REMINDER_15M", "CORRECTION_CONSOLIDATED", "CANCELLATION"
    target_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), default="PENDING", nullable=False, index=True)  # "PENDING", "SENT", "CANCELLED", "SUPERSEDED", "SUPPRESSED"
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    occurrence: Mapped["ClassOccurrence"] = relationship("ClassOccurrence")


class PendingMaterialChange(Base, TimestampMixin):
    """
    Tracks material class changes (time, date, teacher, mode, venue) to enforce
    a 2-minute consolidation window before sending a consolidated correction notification.
    """
    __tablename__ = "pending_material_changes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    class_occurrence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("class_occurrences.id", ondelete="CASCADE"),
        index=True,
        nullable=False
    )
    first_change_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    debounce_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    changes_summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    occurrence: Mapped["ClassOccurrence"] = relationship("ClassOccurrence")


class StudioCommunicationBudget(Base, TimestampMixin):
    """
    Internal budget and allowance tracker per studio.
    Controls over-budget policies and reserves capacity for urgent corrections and cancellations.
    """
    __tablename__ = "studio_communication_budgets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False
    )
    monthly_allowance_inr: Mapped[float] = mapped_column(default=1000.0, nullable=False)
    current_month_usage_inr: Mapped[float] = mapped_column(default=0.0, nullable=False)
    current_month_message_count: Mapped[int] = mapped_column(default=0, nullable=False)
    overage_policy: Mapped[str] = mapped_column(String(50), default="RESERVE_CRITICAL", nullable=False)  # "RESERVE_CRITICAL", "STRICT_SUPPRESS", "ALLOW_OVERAGE"
    alert_level_sent: Mapped[int] = mapped_column(default=0, nullable=False)  # 0, 70, 90, 100

    organization: Mapped["Organization"] = relationship("Organization")



class MeetingEmailDelivery(Base, TimestampMixin):
    """Durable per-recipient Zoom Mail outbox; uncertain submissions need review."""
    __tablename__ = "meeting_email_deliveries"
    __table_args__ = (UniqueConstraint("event_key", "recipient_user_id", name="uq_meeting_mail_event_recipient"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    class_occurrence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("class_occurrences.id", ondelete="CASCADE"), index=True)
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    event_key: Mapped[str] = mapped_column(String(255))
    message_type: Mapped[str] = mapped_column(String(50))
    scheduled_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
