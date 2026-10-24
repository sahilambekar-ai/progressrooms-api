"""Configurable class packages and membership package snapshots."""
from alembic import op
import sqlalchemy as sa
revision = "019_class_packages"
down_revision = "018_zoom_settings"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("session_pricing", sa.Column("packages", sa.JSON(), nullable=True))
    op.add_column("membership_tiers", sa.Column("class_packages", sa.JSON(), nullable=True))
    op.add_column("order_items", sa.Column("total_classes", sa.Integer(), nullable=True))

def downgrade():
    op.drop_column("order_items", "total_classes")
    op.drop_column("membership_tiers", "class_packages")
    op.drop_column("session_pricing", "packages")
