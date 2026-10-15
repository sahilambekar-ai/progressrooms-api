"""Composite indexes for existing schedule and enrollment access paths."""
from alembic import op

revision = "008_query_indexes"
down_revision = "007_student_profiles_pause"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_class_occurrences_session_scheduled", "class_occurrences", ["session_id", "scheduled_start_at"]),
    ("ix_session_enrollments_org_user", "session_enrollments", ["organization_id", "user_id"]),
    ("ix_session_enrollments_session_status", "session_enrollments", ["session_id", "status"]),
)


def upgrade():
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade():
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
