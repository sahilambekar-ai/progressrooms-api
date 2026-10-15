import uuid
from datetime import datetime
from sqlalchemy import String, Text, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base, TimestampMixin

class SupportTicket(Base, TimestampMixin):
    __tablename__ = "support_tickets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    student_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    student_name: Mapped[str] = mapped_column(String(255), nullable=False)
    student_email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), default="GENERAL", nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="OPEN", index=True, nullable=False) # OPEN, IN_PROGRESS, RESOLVED
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_member_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organization_members.id", ondelete="SET NULL"), nullable=True)

    organization: Mapped["Organization"] = relationship()
    student: Mapped["User"] = relationship(foreign_keys=[student_id])
    messages: Mapped[list["SupportMessage"]] = relationship(back_populates="ticket", cascade="all, delete-orphan", order_by="SupportMessage.created_at.asc()")

    __table_args__ = (
        Index("ix_support_tickets_org_status", "organization_id", "status"),
    )

class SupportMessage(Base, TimestampMixin):
    __tablename__ = "support_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True, nullable=False)
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    sender_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sender_role: Mapped[str] = mapped_column(String(50), nullable=False) # STUDENT, STUDIO, TEACHER, SYSTEM
    message: Mapped[str] = mapped_column(Text, nullable=False)

    ticket: Mapped["SupportTicket"] = relationship(back_populates="messages")
