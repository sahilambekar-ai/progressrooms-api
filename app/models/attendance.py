import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base, TimestampMixin
from app.common.enums import AttendanceStatus

class AttendanceRecord(Base, TimestampMixin):
    __tablename__ = "attendance_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    class_occurrence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("class_occurrences.id", ondelete="CASCADE"), index=True, nullable=False)
    session_enrollment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("session_enrollments.id", ondelete="CASCADE"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=AttendanceStatus.ATTENDED.value, nullable=False)
    marked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    occurrence: Mapped["ClassOccurrence"] = relationship(back_populates="attendance_records")
    enrollment: Mapped["SessionEnrollment"] = relationship(back_populates="attendance_records")

    __table_args__ = (
        UniqueConstraint("class_occurrence_id", "session_enrollment_id", name="uq_occurrence_enrollment_attendance"),
    )
