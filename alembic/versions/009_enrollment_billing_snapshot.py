"""Snapshot subscription values on enrollments for stable billing statements."""
from alembic import op
import sqlalchemy as sa

revision = "009_enrollment_billing_snapshot"
down_revision = "008_query_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("session_enrollments", sa.Column("billing_value", sa.Numeric(10, 2), nullable=True))
    op.add_column("session_enrollments", sa.Column("billing_currency", sa.String(10), nullable=True))
    op.add_column("session_enrollments", sa.Column("billing_type", sa.String(50), nullable=True))
    op.add_column("session_enrollments", sa.Column("billing_label", sa.String(255), nullable=True))


def downgrade():
    op.drop_column("session_enrollments", "billing_label")
    op.drop_column("session_enrollments", "billing_type")
    op.drop_column("session_enrollments", "billing_currency")
    op.drop_column("session_enrollments", "billing_value")
