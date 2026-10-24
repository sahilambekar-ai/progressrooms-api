"""Verify a requested Zoom Host ID independently of the account email."""
from alembic import op
import sqlalchemy as sa
revision = "030_zoom_host_identity"
down_revision = "029_dashboard_font"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("zoom_oauth_attempts", sa.Column("expected_host_id", sa.String(255)))


def downgrade():
    op.drop_column("zoom_oauth_attempts", "expected_host_id")
