"""Capture declared billing country and join-time pricing eligibility."""
from alembic import op
import sqlalchemy as sa

revision = "028_usage_pricing_region"
down_revision = "027_saas_checkout"
branch_labels = None
depends_on = None


def upgrade():
    # Historical country cannot be inferred from phone, timezone or currency.
    op.add_column("users", sa.Column("billing_country", sa.String(2)))
    op.add_column("student_join_events", sa.Column("pricing_region", sa.String(20)))


def downgrade():
    op.drop_column("student_join_events", "pricing_region")
    op.drop_column("users", "billing_country")
