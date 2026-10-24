"""Qualifying student joins and monthly platform invoices."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "026_active_student_billing"
down_revision = "025_zoom_host_key"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("student_join_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("enrollment_id", UUID(as_uuid=True), sa.ForeignKey("session_enrollments.id"), nullable=False),
        sa.Column("occurrence_id", UUID(as_uuid=True), sa.ForeignKey("class_occurrences.id"), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_student_join_studio_month_user", "student_join_events", ["organization_id", "month", "user_id"])
    op.create_table("student_join_teachers",
        sa.Column("event_id", UUID(as_uuid=True), sa.ForeignKey("student_join_events.id"), primary_key=True),
        sa.Column("teacher_id", UUID(as_uuid=True), sa.ForeignKey("organization_members.id"), primary_key=True))
    op.create_index("ix_student_join_teachers_teacher_id", "student_join_teachers", ["teacher_id"])
    op.create_table("platform_invoices",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("active_students", sa.Integer(), nullable=False),
        sa.Column("free_limit", sa.Integer(), nullable=False),
        sa.Column("rate", sa.Numeric(10, 2), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "month", name="uq_platform_invoice_studio_month"))
    op.create_index("ix_platform_invoices_organization_id", "platform_invoices", ["organization_id"])


def downgrade():
    op.drop_table("platform_invoices")
    op.drop_table("student_join_teachers")
    op.drop_table("student_join_events")
