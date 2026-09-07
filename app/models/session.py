import uuid
from sqlalchemy import String, Boolean, Text, Integer, Numeric, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base, TimestampMixin, SoftDeleteMixin
from app.common.enums import SessionType, SkillLevel, PricingType, InstructorRole

class ClassType(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "class_types"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="class_types")
    sessions: Mapped[list["Session"]] = relationship(back_populates="class_type", cascade="all, delete-orphan")

class Session(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    class_type_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("class_types.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    short_description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    full_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_type: Mapped[str] = mapped_column(String(50), default=SessionType.GROUP.value, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    min_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skill_level: Mapped[str] = mapped_column(String(50), default=SkillLevel.ALL_LEVELS.value, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    language: Mapped[str] = mapped_column(String(50), default="English", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    meeting_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="sessions")
    class_type: Mapped["ClassType"] = relationship(back_populates="sessions")
    instructors: Mapped[list["SessionInstructor"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    pricing: Mapped["SessionPricing"] = relationship(back_populates="session", uselist=False, cascade="all, delete-orphan")
    requirements: Mapped["SessionRequirement"] = relationship(back_populates="session", uselist=False, cascade="all, delete-orphan")
    schedule_rules: Mapped[list["SessionScheduleRule"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    occurrences: Mapped[list["ClassOccurrence"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    enrollments: Mapped[list["SessionEnrollment"]] = relationship(back_populates="session")

class SessionInstructor(Base, TimestampMixin):
    __tablename__ = "session_instructors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False)
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organization_members.id", ondelete="CASCADE"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(50), default=InstructorRole.PRIMARY.value, nullable=False)

    session: Mapped["Session"] = relationship(back_populates="instructors")
    member: Mapped["OrganizationMember"] = relationship()

class SessionPricing(Base, TimestampMixin):
    __tablename__ = "session_pricing"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), unique=True, nullable=False)
    pricing_type: Mapped[str] = mapped_column(String(50), default=PricingType.MONTHLY.value, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)
    total_classes: Mapped[int | None] = mapped_column(Integer, nullable=True) # For package
    validity_days: Mapped[int | None] = mapped_column(Integer, default=30, nullable=True) # Access window

    session: Mapped["Session"] = relationship(back_populates="pricing")

class SessionRequirement(Base, TimestampMixin):
    __tablename__ = "session_requirements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), unique=True, nullable=False)
    prerequisites: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_equipment: Mapped[str | None] = mapped_column(Text, nullable=True)
    preparation_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    additional_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    session: Mapped["Session"] = relationship(back_populates="requirements")
