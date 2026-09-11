"""Add Studio Profile, Location, GST, Bank and Zoom fields to organization_settings

Revision ID: 002_studio_profile_and_zoom
Revises: 001_initial_schema
Create Date: 2026-09-11 18:37:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '002_studio_profile_and_zoom'
down_revision: Union[str, None] = '001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.add_column('organization_settings', sa.Column('phone', sa.String(50), nullable=True))
    op.add_column('organization_settings', sa.Column('studio_tagline', sa.String(255), nullable=True))
    op.add_column('organization_settings', sa.Column('disciplines', sa.String(500), nullable=True))
    op.add_column('organization_settings', sa.Column('teaching_mode', sa.String(50), server_default='HYBRID', nullable=False))
    op.add_column('organization_settings', sa.Column('address_line1', sa.String(255), nullable=True))
    op.add_column('organization_settings', sa.Column('address_line2', sa.String(255), nullable=True))
    op.add_column('organization_settings', sa.Column('city', sa.String(100), nullable=True))
    op.add_column('organization_settings', sa.Column('state', sa.String(100), nullable=True))
    op.add_column('organization_settings', sa.Column('pincode', sa.String(20), nullable=True))
    op.add_column('organization_settings', sa.Column('country', sa.String(100), server_default='India', nullable=False))
    op.add_column('organization_settings', sa.Column('has_gst', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('organization_settings', sa.Column('gst_number', sa.String(20), nullable=True))
    op.add_column('organization_settings', sa.Column('legal_business_name', sa.String(255), nullable=True))
    op.add_column('organization_settings', sa.Column('pan_number', sa.String(20), nullable=True))
    op.add_column('organization_settings', sa.Column('bank_name', sa.String(100), nullable=True))
    op.add_column('organization_settings', sa.Column('account_holder_name', sa.String(255), nullable=True))
    op.add_column('organization_settings', sa.Column('account_number_enc', sa.String(255), nullable=True))
    op.add_column('organization_settings', sa.Column('ifsc_code', sa.String(20), nullable=True))
    op.add_column('organization_settings', sa.Column('upi_id', sa.String(100), nullable=True))
    op.add_column('organization_settings', sa.Column('settlement_cycle', sa.String(50), server_default='WEEKLY', nullable=False))
    op.add_column('organization_settings', sa.Column('zoom_account_email', sa.String(255), nullable=True))
    op.add_column('organization_settings', sa.Column('zoom_auto_meeting_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('organization_settings', sa.Column('zoom_waiting_room', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('organization_settings', sa.Column('zoom_host_video', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('organization_settings', sa.Column('account_completed', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('organization_settings', sa.Column('completion_step', sa.Integer(), server_default=sa.text('1'), nullable=False))

def downgrade() -> None:
    columns = [
        'phone', 'studio_tagline', 'disciplines', 'teaching_mode',
        'address_line1', 'address_line2', 'city', 'state', 'pincode', 'country',
        'has_gst', 'gst_number', 'legal_business_name', 'pan_number',
        'bank_name', 'account_holder_name', 'account_number_enc', 'ifsc_code', 'upi_id',
        'settlement_cycle', 'zoom_account_email', 'zoom_auto_meeting_enabled',
        'zoom_waiting_room', 'zoom_host_video', 'account_completed', 'completion_step'
    ]
    for col in columns:
        op.drop_column('organization_settings', col)
