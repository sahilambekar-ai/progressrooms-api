import uuid
from datetime import time, date, datetime, timezone
from sqlalchemy import String, Boolean, Text, Integer, Time, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base, TimestampMixin
from app.common.enums import OccurrenceStatus

class SessionScheduleRule(Base, TimestampMixin):
    __tablename__ = "session_schedule_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False) # 0=Monday, 6=Sunday
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), default="Asia/Kolkata", nullable=False)
    effective_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    session: Mapped["Session"] = relationship(back_populates="schedule_rules")

class ClassOccurrence(Base, TimestampMixin):
    __tablename__ = "class_occurrences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    instructor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organization_members.id", ondelete="SET NULL"), index=True, nullable=True)
    scheduled_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduled_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    actual_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=OccurrenceStatus.SCHEDULED.value, index=True, nullable=False)
    meeting_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    host_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    meeting_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_meeting_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zoom_connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    zoom_connection_version: Mapped[int | None] = mapped_column(Integer)
    zoom_host_member_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    zoom_host_email: Mapped[str | None] = mapped_column(String(255))
    zoom_provisioning_status: Mapped[str] = mapped_column(String(40), default="NOT_PROVISIONED", nullable=False)
    zoom_operation_key: Mapped[str | None] = mapped_column(String(100))
    zoom_settings_snapshot: Mapped[dict | None] = mapped_column(JSON)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    session: Mapped["Session"] = relationship(back_populates="occurrences")
    instructor: Mapped["OrganizationMember"] = relationship(foreign_keys=[instructor_id])
    changes: Mapped[list["ClassOccurrenceChange"]] = relationship(back_populates="occurrence", cascade="all, delete-orphan")
    attendance_records: Mapped[list["AttendanceRecord"]] = relationship(back_populates="occurrence", cascade="all, delete-orphan")

    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "zoom_connection_id"], ["teacher_zoom_connections.organization_id", "teacher_zoom_connections.id"], name="fk_occ_zoom_connection"),
        ForeignKeyConstraint(["organization_id", "zoom_host_member_id"], ["organization_members.organization_id", "organization_members.id"], name="fk_occ_zoom_host"),
        Index("ix_class_occurrences_org_time", "organization_id", "actual_start_at"),
        Index("ix_class_occurrences_session_scheduled", "session_id", "scheduled_start_at"),
    )

class ClassOccurrenceChange(Base):
    __tablename__ = "class_occurrence_changes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    class_occurrence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("class_occurrences.id", ondelete="CASCADE"), index=True, nullable=False)
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    change_type: Mapped[str] = mapped_column(String(50), nullable=False) # RESCHEDULE, CANCEL, INSTRUCTOR_CHANGE
    previous_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    occurrence: Mapped["ClassOccurrence"] = relationship(back_populates="changes")
