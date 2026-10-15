"""Add optional membership offer prices with database validation."""
from alembic import op
import sqlalchemy as sa

revision = "013_membership_offer_price"
down_revision = "012_public_storefront"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("membership_tiers") as batch:
        batch.add_column(sa.Column("offer_price", sa.Numeric(10, 2), nullable=True))
        batch.create_check_constraint("ck_membership_tiers_price_nonnegative", "price >= 0")
        batch.create_check_constraint(
            "ck_membership_tiers_offer_price",
            "offer_price IS NULL OR (offer_price >= 0 AND offer_price <= price)",
        )


def downgrade():
    with op.batch_alter_table("membership_tiers") as batch:
        batch.drop_constraint("ck_membership_tiers_offer_price", type_="check")
        batch.drop_constraint("ck_membership_tiers_price_nonnegative", type_="check")
        batch.drop_column("offer_price")
