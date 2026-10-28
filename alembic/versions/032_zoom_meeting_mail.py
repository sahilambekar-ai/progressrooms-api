"""Durable per-recipient Zoom Mail queue; retain historical WhatsApp data."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "032_zoom_meeting_mail"
down_revision = "031_communication_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("meeting_email_deliveries",
        sa.Column("id", postgresql.UUID(), primary_key=True, nullable=False),
        sa.Column("class_occurrence_id", postgresql.UUID(), sa.ForeignKey("class_occurrences.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_key", sa.String(255), nullable=False),
        sa.Column("message_type", sa.String(50), nullable=False),
        sa.Column("scheduled_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_key", "recipient_user_id", name="uq_meeting_mail_event_recipient"))
    for field in ("class_occurrence_id", "status", "available_at"):
        op.create_index(f"ix_meeting_email_deliveries_{field}", "meeting_email_deliveries", [field])
    op.execute(sa.text("UPDATE class_reminder_schedules SET status='SUPERSEDED' WHERE status='PENDING' AND reminder_type IN ('REMINDER_2H', 'REMINDER_15M')"))
    op.execute(sa.text("UPDATE platform_communication_settings SET whatsapp_enabled=false"))


def downgrade():
    op.drop_table("meeting_email_deliveries")
