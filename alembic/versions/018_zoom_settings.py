"""Add zoom settings and host key columns to organization_settings.

Revision ID: 018_zoom_settings
Revises: 016_consolidate_passwords
Create Date: 2026-10-20
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '018_zoom_settings'
down_revision: Union[str, None] = '016_consolidate_passwords'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_COLUMNS = [
    ("zoom_enforce_basic_login", sa.Column("zoom_enforce_basic_login", sa.Boolean(), server_default=sa.text("true"), nullable=False)),
    ("zoom_enforce_zoom_login", sa.Column("zoom_enforce_zoom_login", sa.Boolean(), server_default=sa.text("false"), nullable=False)),
    ("zoom_group_allow_self_buy_subscriptions", sa.Column("zoom_group_allow_self_buy_subscriptions", sa.Boolean(), server_default=sa.text("true"), nullable=False)),
    ("zoom_group_active_subscription_required", sa.Column("zoom_group_active_subscription_required", sa.Boolean(), server_default=sa.text("true"), nullable=False)),
    ("zoom_group_confirm_checkin_everytime", sa.Column("zoom_group_confirm_checkin_everytime", sa.Boolean(), server_default=sa.text("true"), nullable=False)),
    ("zoom_personal_allow_self_buy_subscriptions", sa.Column("zoom_personal_allow_self_buy_subscriptions", sa.Boolean(), server_default=sa.text("true"), nullable=False)),
    ("zoom_personal_active_subscription_required", sa.Column("zoom_personal_active_subscription_required", sa.Boolean(), server_default=sa.text("true"), nullable=False)),
    ("zoom_personal_confirm_checkin_everytime", sa.Column("zoom_personal_confirm_checkin_everytime", sa.Boolean(), server_default=sa.text("true"), nullable=False)),
    ("zoom_personal_no_show_penalty", sa.Column("zoom_personal_no_show_penalty", sa.Boolean(), server_default=sa.text("true"), nullable=False)),
    ("zoom_join_before_host", sa.Column("zoom_join_before_host", sa.Boolean(), server_default=sa.text("true"), nullable=False)),
    ("zoom_cloud_recording_mode", sa.Column("zoom_cloud_recording_mode", sa.String(50), server_default="DO_NOT_SHARE", nullable=False)),
    ("zoom_host_key", sa.Column("zoom_host_key", sa.String(50), nullable=True)),
]

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "organization_settings" in tables:
        existing_cols = {c["name"] for c in inspector.get_columns("organization_settings")}
        for col_name, col_def in NEW_COLUMNS:
            if col_name not in existing_cols:
                op.add_column("organization_settings", col_def)

def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "organization_settings" in tables:
        existing_cols = {c["name"] for c in inspector.get_columns("organization_settings")}
        for col_name, _ in reversed(NEW_COLUMNS):
            if col_name in existing_cols:
                op.drop_column("organization_settings", col_name)
