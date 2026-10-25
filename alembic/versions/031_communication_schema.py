"""Create the missing operational communication tables (frozen schema)."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "031_communication_schema"
down_revision = "030_zoom_host_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('platform_communication_settings',
        sa.Column('id', postgresql.UUID(), primary_key=True, nullable=False),
        sa.Column('whatsapp_enabled', sa.Boolean(), nullable=False),
        sa.Column('email_enabled', sa.Boolean(), nullable=False),
        sa.Column('updated_by_user_id', postgresql.UUID(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table('communication_channel_audit_logs',
        sa.Column('id', postgresql.UUID(), primary_key=True, nullable=False),
        sa.Column('channel', sa.String(length=50), nullable=False),
        sa.Column('previous_state', sa.Boolean(), nullable=False),
        sa.Column('new_state', sa.Boolean(), nullable=False),
        sa.Column('changed_by_user_id', postgresql.UUID(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('changed_by_email', sa.String(length=255), nullable=True),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
    )
    op.create_table('communication_logs',
        sa.Column('id', postgresql.UUID(), primary_key=True, nullable=False),
        sa.Column('recipient_user_id', postgresql.UUID(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('organization_id', postgresql.UUID(), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=True),
        sa.Column('recipient_email', sa.String(length=255), nullable=True),
        sa.Column('recipient_phone', sa.String(length=50), nullable=True),
        sa.Column('channel', sa.String(length=50), nullable=False),
        sa.Column('message_type', sa.String(length=100), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('suppression_reason', sa.String(length=255), nullable=True),
        sa.Column('subject_or_title', sa.String(length=500), nullable=True),
        sa.Column('body_preview', sa.Text(), nullable=True),
        sa.Column('metadata_json', postgresql.JSONB(), nullable=True),
        sa.Column('error_details', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_communication_logs_channel', 'communication_logs', ['channel'], unique=False)
    op.create_index('ix_communication_logs_created_at', 'communication_logs', ['created_at'], unique=False)
    op.create_index('ix_communication_logs_message_type', 'communication_logs', ['message_type'], unique=False)
    op.create_index('ix_communication_logs_organization_id', 'communication_logs', ['organization_id'], unique=False)
    op.create_index('ix_communication_logs_recipient_email', 'communication_logs', ['recipient_email'], unique=False)
    op.create_index('ix_communication_logs_recipient_phone', 'communication_logs', ['recipient_phone'], unique=False)
    op.create_index('ix_communication_logs_recipient_user_id', 'communication_logs', ['recipient_user_id'], unique=False)
    op.create_index('ix_communication_logs_status', 'communication_logs', ['status'], unique=False)
    op.create_table('user_communication_preferences',
        sa.Column('id', postgresql.UUID(), primary_key=True, nullable=False),
        sa.Column('user_id', postgresql.UUID(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('whatsapp_opted_out', sa.Boolean(), nullable=False),
        sa.Column('email_opted_out', sa.Boolean(), nullable=False),
        sa.Column('opted_out_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_user_communication_preferences_user_id', 'user_communication_preferences', ['user_id'], unique=True)
    op.create_table('class_reminder_schedules',
        sa.Column('id', postgresql.UUID(), primary_key=True, nullable=False),
        sa.Column('class_occurrence_id', postgresql.UUID(), sa.ForeignKey('class_occurrences.id', ondelete='CASCADE'), nullable=False),
        sa.Column('schedule_version', sa.Integer(), nullable=False),
        sa.Column('reminder_type', sa.String(length=50), nullable=False),
        sa.Column('target_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('details_snapshot', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_class_reminder_schedules_class_occurrence_id', 'class_reminder_schedules', ['class_occurrence_id'], unique=False)
    op.create_index('ix_class_reminder_schedules_status', 'class_reminder_schedules', ['status'], unique=False)
    op.create_index('ix_class_reminder_schedules_target_time', 'class_reminder_schedules', ['target_time'], unique=False)
    op.create_table('pending_material_changes',
        sa.Column('id', postgresql.UUID(), primary_key=True, nullable=False),
        sa.Column('class_occurrence_id', postgresql.UUID(), sa.ForeignKey('class_occurrences.id', ondelete='CASCADE'), nullable=False),
        sa.Column('first_change_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('debounce_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('changes_summary', postgresql.JSONB(), nullable=False),
        sa.Column('processed', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_pending_material_changes_class_occurrence_id', 'pending_material_changes', ['class_occurrence_id'], unique=False)
    op.create_table('studio_communication_budgets',
        sa.Column('id', postgresql.UUID(), primary_key=True, nullable=False),
        sa.Column('organization_id', postgresql.UUID(), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('monthly_allowance_inr', sa.Float(), nullable=False),
        sa.Column('current_month_usage_inr', sa.Float(), nullable=False),
        sa.Column('current_month_message_count', sa.Integer(), nullable=False),
        sa.Column('overage_policy', sa.String(length=50), nullable=False),
        sa.Column('alert_level_sent', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_studio_communication_budgets_organization_id', 'studio_communication_budgets', ['organization_id'], unique=True)


def downgrade():
    op.drop_table('studio_communication_budgets')
    op.drop_table('pending_material_changes')
    op.drop_table('class_reminder_schedules')
    op.drop_table('user_communication_preferences')
    op.drop_table('communication_logs')
    op.drop_table('communication_channel_audit_logs')
    op.drop_table('platform_communication_settings')
