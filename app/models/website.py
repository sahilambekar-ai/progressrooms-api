import uuid
from sqlalchemy import String, Boolean, Text, BigInteger, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base, TimestampMixin, SoftDeleteMixin
from app.common.enums import WebsiteTemplateCode

class WebsiteTemplate(Base, TimestampMixin):
    __tablename__ = "website_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, default=WebsiteTemplateCode.DEFAULT.value, nullable=False)
    preview_image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

class OrganizationWebsite(Base, TimestampMixin):
    __tablename__ = "organization_websites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), unique=True, nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("website_templates.id"), nullable=False)
    headline: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subheadline: Mapped[str | None] = mapped_column(String(500), nullable=True)
    hero_image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    about_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_color: Mapped[str] = mapped_column(String(20), default="#4f46e5", nullable=False)
    accent_color: Mapped[str] = mapped_column(String(20), default="#06b6d4", nullable=False)
    social_links: Mapped[dict | None] = mapped_column(JSONB, default=dict, nullable=True)
    custom_sections: Mapped[list | None] = mapped_column(JSONB, default=list, nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    organization: Mapped["Organization"] = relationship(back_populates="website")
    template: Mapped["WebsiteTemplate"] = relationship()

class OrganizationDomain(Base, TimestampMixin):
    __tablename__ = "organization_domains"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    domain_name: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ssl_status: Mapped[str] = mapped_column(String(50), default="PENDING", nullable=False)

class MediaAsset(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "media_assets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
