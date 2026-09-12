"""Add completion_percentage and whatsapp_number

Revision ID: 003_completion_percentage
Revises: 002_studio_profile_and_zoom
Create Date: 2026-09-12 09:18:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '003_completion_percentage'
down_revision: Union[str, None] = '002_studio_profile_and_zoom'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.add_column('organization_settings', sa.Column('whatsapp_number', sa.String(50), nullable=True))
    op.add_column('organization_settings', sa.Column('completion_percentage', sa.Integer(), server_default=sa.text('0'), nullable=False))

def downgrade() -> None:
    op.drop_column('organization_settings', 'completion_percentage')
    op.drop_column('organization_settings', 'whatsapp_number')
