"""SaaS checkout snapshots and trial management; legacy access is unchanged."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime, timezone
import uuid

revision = "027_saas_checkout"
down_revision = "026_active_student_billing"
branch_labels = None
depends_on = None


def upgrade():
    # Registration now needs a real plan FK. Preserve existing catalog prices.
    plans = sa.table("plans", sa.column("id", UUID(as_uuid=True)), sa.column("slug", sa.String()),
        sa.column("name", sa.String()), sa.column("price", sa.Numeric()), sa.column("currency", sa.String()),
        sa.column("billing_interval", sa.String()), sa.column("status", sa.String()), sa.column("is_public", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)), sa.column("updated_at", sa.DateTime(timezone=True)))
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    for slug, name, price in [("starter", "Starter", 0), ("core", "Core", 1499), ("studio", "Studio", 3999)]:
        if not bind.scalar(sa.select(plans.c.id).where(plans.c.slug == slug)):
            bind.execute(plans.insert().values(id=uuid.uuid4(), slug=slug, name=name, price=price,
                currency="INR", billing_interval="MONTHLY", status="ACTIVE", is_public=True,
                created_at=now, updated_at=now))
    op.add_column("organization_subscriptions", sa.Column("saas_managed", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("organization_subscriptions", sa.Column("trial_grace_ends_at", sa.DateTime(timezone=True)))
    op.create_table("saas_checkouts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("purchaser_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("plan_id", UUID(as_uuid=True), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("total_paise", sa.Integer(), nullable=False),
        sa.Column("quote", sa.JSON(), nullable=False),
        sa.Column("provider_key_id", sa.String(255), nullable=False),
        sa.Column("provider_order_id", sa.String(255), unique=True),
        sa.Column("provider_payment_id", sa.String(255), unique=True),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("service_starts_at", sa.DateTime(timezone=True)),
        sa.Column("service_ends_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "idempotency_key", name="uq_saas_checkout_retry"),
        sa.CheckConstraint("total_paise > 0", name="ck_saas_positive_total"))
    op.create_index("ix_saas_checkouts_organization_id", "saas_checkouts", ["organization_id"])
    op.create_index("ix_saas_checkouts_status", "saas_checkouts", ["status"])
    op.create_table("saas_webhook_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", sa.String(255), nullable=False, unique=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("saas_webhook_events")
    op.drop_table("saas_checkouts")
    op.drop_column("organization_subscriptions", "trial_grace_ends_at")
    op.drop_column("organization_subscriptions", "saas_managed")
