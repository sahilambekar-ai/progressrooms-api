"""Persist owner-managed teacher attendance per class occurrence."""
from alembic import op
import sqlalchemy as sa

revision = "014_teacher_attendance"
down_revision = "013_membership_offer_price"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("teacher_attendance",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("class_occurrence_id", sa.UUID(), sa.ForeignKey("class_occurrences.id", ondelete="CASCADE"), nullable=False),
        sa.Column("teacher_member_id", sa.UUID(), sa.ForeignKey("organization_members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("marked_by_user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("class_occurrence_id", "teacher_member_id", name="uq_teacher_occurrence_attendance"),
        sa.CheckConstraint("status IN ('PRESENT', 'ABSENT', 'CANCELLED')", name="ck_teacher_attendance_status"))
    op.create_index("ix_teacher_attendance_class_occurrence_id", "teacher_attendance", ["class_occurrence_id"])
    op.create_index("ix_teacher_attendance_teacher_member_id", "teacher_attendance", ["teacher_member_id"])


def downgrade():
    op.drop_table("teacher_attendance")
