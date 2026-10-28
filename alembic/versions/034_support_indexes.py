"""Align support indexes with metadata and immutable student ownership queries.

Transactional DDL preserves the old schema on failure. Use a maintenance window
for large support tables: these ordinary index operations briefly block writers.
"""
from alembic import op
import sqlalchemy as sa

revision = '034_support_indexes'
down_revision = '033_auth_security'
branch_labels = None
depends_on = None


def _bounds():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))


def _rename(old, new):
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(sa.text(f'ALTER INDEX {old} RENAME TO {new}'))
    else:
        op.drop_index(old, table_name='support_tickets')
        op.create_index(new, 'support_tickets', ['organization_id'])


def upgrade():
    _bounds()
    _rename('ix_support_tickets_org_id', 'ix_support_tickets_organization_id')
    op.create_index('ix_support_tickets_student_id', 'support_tickets', ['student_id'])


def downgrade():
    _bounds()
    op.drop_index('ix_support_tickets_student_id', table_name='support_tickets')
    _rename('ix_support_tickets_organization_id', 'ix_support_tickets_org_id')
