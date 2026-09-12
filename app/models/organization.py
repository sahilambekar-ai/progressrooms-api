import uuid
from sqlalchemy import String, Boolean, Text, Integer, ForeignKey, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base, TimestampMixin, SoftDeleteMixin
from app.common.enums import UserRole, OrgStatus

class Organization(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    logo_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    timezone: Mapped[str] = mapped_column(String(100), default="Asia/Kolkata", nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)
    status: Mapped[str] = mapped_column(String(50), default=OrgStatus.ACTIVE.value, nullable=False)

    members: Mapped[list["OrganizationMember"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    subscription: Mapped["OrganizationSubscription"] = relationship(back_populates="organization", uselist=False)
    class_types: Mapped[list["ClassType"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    sessions: Mapped[list["Session"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    website: Mapped["OrganizationWebsite"] = relationship(back_populates="organization", uselist=False)
    settings: Mapped["OrganizationSetting"] = relationship(back_populates="organization", uselist=False)

class OrganizationMember(Base, TimestampMixin):
    __tablename__ = "organization_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(50), default=UserRole.INSTRUCTOR.value, nullable=False)
    title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    organization: Mapped["Organization"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")

class OrganizationStudent(Base, TimestampMixin):
    __tablename__ = "organization_students"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    student_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE", nullable=False)

class OrganizationSetting(Base, TimestampMixin):
    __tablename__ = "organization_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), unique=True, nullable=False)
    brand_color: Mapped[str | None] = mapped_column(String(20), default="#4f46e5", nullable=True)
    support_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    support_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    
    # Studio Profile & Contact
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    whatsapp_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    studio_tagline: Mapped[str | None] = mapped_column(String(255), nullable=True)
    disciplines: Mapped[str | None] = mapped_column(String(500), nullable=True) # Comma-separated or tags

    # Studio Location & Teaching Mode
    teaching_mode: Mapped[str] = mapped_column(String(50), default="HYBRID", nullable=False) # PHYSICAL, ONLINE, HYBRID
    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pincode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str] = mapped_column(String(100), default="India", nullable=False)

    # Tax & Legal Details
    has_gst: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    gst_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    legal_business_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pan_number: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Bank & Payout Accounts (Student Fees Settlements)
    bank_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    account_holder_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_number_enc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ifsc_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    upi_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    settlement_cycle: Mapped[str] = mapped_column(String(50), default="WEEKLY", nullable=False)

    # Zoom Authentication & Classroom Preferences
    zoom_connected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    zoom_account_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zoom_auto_meeting_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    zoom_waiting_room: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    zoom_host_video: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Onboarding Status
    account_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    completion_step: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    completion_percentage: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    organization: Mapped["Organization"] = relationship(back_populates="settings")
