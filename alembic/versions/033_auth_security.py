"""Account session revocation and shared authentication abuse counters."""
from alembic import op
import sqlalchemy as sa

revision = '033_auth_security'
down_revision = '032_zoom_meeting_mail'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('session_version', sa.Integer(), nullable=False, server_default='0'))
    op.create_table('auth_rate_limits',
        sa.Column('key', sa.String(64), primary_key=True),
        sa.Column('window_start', sa.Integer(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.Integer(), nullable=False))
    op.create_index('ix_auth_rate_limits_expires_at', 'auth_rate_limits', ['expires_at'])


def downgrade():
    raise RuntimeError(
        "Auth security cannot be safely downgraded: old code cannot verify Argon2 "
        "and would accept revoked tokens. Use a compatible forward fix; disaster "
        "recovery requires a coherent backup and signing-key rotation."
    )
