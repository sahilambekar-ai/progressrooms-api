"""Persist each user's dashboard font, with IBM Plex Sans as the default."""
from alembic import op
import sqlalchemy as sa

revision = "029_dashboard_font"
down_revision = "028_usage_pricing_region"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("dashboard_font", sa.String(20), nullable=False, server_default="ibm-plex"))


def downgrade():
    op.drop_column("users", "dashboard_font")
