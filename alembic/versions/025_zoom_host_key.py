"""Encrypted host key tied to the authenticated Zoom account."""
from alembic import op
import sqlalchemy as sa
revision = "025_zoom_host_key"
down_revision = "024_studio_onboarding"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("teacher_zoom_connections", sa.Column("host_key_enc", sa.Text(), nullable=True))

def downgrade():
    op.drop_column("teacher_zoom_connections", "host_key_enc")
