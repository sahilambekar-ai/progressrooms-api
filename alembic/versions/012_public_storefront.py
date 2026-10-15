"""Snapshot storefront purchases and share pass credits across classes."""
from alembic import op
import sqlalchemy as sa
revision = "012_public_storefront"
down_revision = "011_require_teacher_codes"
branch_labels = None
depends_on = None

def upgrade():
    for name, kind in [("provider_order_id", sa.String(255)), ("offering_label", sa.String(255)), ("offering_type", sa.String(50)), ("credit_limit", sa.Integer()), ("validity_days", sa.Integer())]:
        op.add_column("orders", sa.Column(name, kind, nullable=True))
    op.create_unique_constraint("uq_orders_provider_order_id", "orders", ["provider_order_id"])
    op.add_column("session_enrollments", sa.Column("purchase_order_id", sa.UUID(), nullable=True))
    op.create_foreign_key("fk_enrollment_purchase", "session_enrollments", "orders", ["purchase_order_id"], ["id"])
    op.create_index("ix_session_enrollments_purchase_order_id", "session_enrollments", ["purchase_order_id"])

def downgrade():
    op.drop_index("ix_session_enrollments_purchase_order_id", "session_enrollments")
    op.drop_constraint("fk_enrollment_purchase", "session_enrollments", type_="foreignkey")
    op.drop_column("session_enrollments", "purchase_order_id")
    op.drop_constraint("uq_orders_provider_order_id", "orders", type_="unique")
    for name in ["provider_order_id", "offering_label", "offering_type", "credit_limit", "validity_days"]:
        op.drop_column("orders", name)
