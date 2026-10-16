"""Add meeting details to class occurrences and create support ticket tables."""
from alembic import op
import sqlalchemy as sa

revision = "015_meetings_and_support"
down_revision = "014_teacher_attendance"
branch_labels = None
depends_on = None

def upgrade():
    # 1. Add meeting provider, host_url, provider_meeting_id to class_occurrences
    op.add_column("class_occurrences", sa.Column("host_url", sa.String(1024), nullable=True))
    op.add_column("class_occurrences", sa.Column("meeting_provider", sa.String(50), nullable=True))
    op.add_column("class_occurrences", sa.Column("provider_meeting_id", sa.String(255), nullable=True))

    # 2. Create support_tickets table
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("student_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("student_name", sa.String(255), nullable=False),
        sa.Column("student_email", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("category", sa.String(100), server_default="GENERAL", nullable=False),
        sa.Column("status", sa.String(50), server_default="OPEN", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_member_id", sa.UUID(), sa.ForeignKey("organization_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_support_tickets_org_id", "support_tickets", ["organization_id"])
    op.create_index("ix_support_tickets_student_email", "support_tickets", ["student_email"])
    op.create_index("ix_support_tickets_status", "support_tickets", ["status"])
    op.create_index("ix_support_tickets_org_status", "support_tickets", ["organization_id", "status"])

    # 3. Create support_messages table
    op.create_table(
        "support_messages",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("ticket_id", sa.UUID(), sa.ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sender_name", sa.String(255), nullable=False),
        sa.Column("sender_role", sa.String(50), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_support_messages_ticket_id", "support_messages", ["ticket_id"])

def downgrade():
    op.drop_table("support_messages")
    op.drop_table("support_tickets")
    op.drop_column("class_occurrences", "provider_meeting_id")
    op.drop_column("class_occurrences", "meeting_provider")
    op.drop_column("class_occurrences", "host_url")
