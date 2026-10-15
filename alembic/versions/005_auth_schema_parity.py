"""Make historical manually-added authentication fields reproducible on fresh databases."""
from alembic import op
import sqlalchemy as sa

revision = '005_auth_schema_parity'
down_revision = '004_account_roles'
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    columns = {c['name'] for c in sa.inspect(bind).get_columns('users')}
    for column in [sa.Column('password_hash', sa.String(255)),
                   sa.Column('is_verified', sa.Boolean(), nullable=False, server_default=sa.false()),
                   sa.Column('account_status', sa.String(50), nullable=False, server_default='ACTIVE'),
                   sa.Column('last_login_at', sa.DateTime(timezone=True))]:
        if column.name not in columns:
            op.add_column('users', column)
    if not sa.inspect(bind).has_table('user_otps'):
        op.create_table('user_otps',
            sa.Column('id', sa.UUID(), primary_key=True),
            sa.Column('user_id', sa.UUID(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('email', sa.String(255), nullable=False),
            sa.Column('otp_hash', sa.String(255), nullable=False),
            sa.Column('purpose', sa.String(50), nullable=False),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('verified_at', sa.DateTime(timezone=True)),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
        op.create_index('ix_user_otps_user_id', 'user_otps', ['user_id'])
        op.create_index('ix_user_otps_email', 'user_otps', ['email'])

def downgrade():
    # Preserve authentication data that may predate this repair migration.
    pass
