"""Multiline studio introduction and onboarding defaults."""
from alembic import op
import sqlalchemy as sa
revision = "024_studio_onboarding"
down_revision = "023_flexible_class_capacity"
branch_labels = None
depends_on = None

def upgrade():
    op.alter_column("organization_settings", "studio_tagline", existing_type=sa.String(255), type_=sa.Text())

def downgrade():
    op.execute("UPDATE organization_settings SET studio_tagline = LEFT(studio_tagline, 255)")
    op.alter_column("organization_settings", "studio_tagline", existing_type=sa.Text(), type_=sa.String(255))
