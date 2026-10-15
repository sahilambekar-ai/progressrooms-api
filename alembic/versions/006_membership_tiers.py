"""Create membership_tiers and membership_tier_classes tables."""
from alembic import op
import sqlalchemy as sa

revision = '006_membership_tiers'
down_revision = '005_auth_schema_parity'
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table('membership_tiers'):
        op.create_table(
            'membership_tiers',
            sa.Column('id', sa.UUID(), primary_key=True),
            sa.Column('organization_id', sa.UUID(), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
            sa.Column('name', sa.String(255), nullable=False),
            sa.Column('slug', sa.String(255), nullable=False),
            sa.Column('tier_type', sa.String(50), nullable=False, server_default='MONTHLY'),
            sa.Column('price', sa.Numeric(10, 2), nullable=False),
            sa.Column('currency', sa.String(10), nullable=False, server_default='INR'),
            sa.Column('validity_days', sa.Integer(), nullable=False, server_default='30'),
            sa.Column('total_classes', sa.Integer(), nullable=True),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_membership_tiers_organization_id', 'membership_tiers', ['organization_id'])

    if not inspector.has_table('membership_tier_classes'):
        op.create_table(
            'membership_tier_classes',
            sa.Column('id', sa.UUID(), primary_key=True),
            sa.Column('tier_id', sa.UUID(), sa.ForeignKey('membership_tiers.id', ondelete='CASCADE'), nullable=False),
            sa.Column('session_id', sa.UUID(), sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index('ix_membership_tier_classes_tier_id', 'membership_tier_classes', ['tier_id'])
        op.create_index('ix_membership_tier_classes_session_id', 'membership_tier_classes', ['session_id'])

def downgrade():
    op.drop_table('membership_tier_classes')
    op.drop_table('membership_tiers')
