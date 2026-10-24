"""Private teacher OAuth grants, scoped attempts and persisted meeting hosts."""
from alembic import op
import sqlalchemy as sa

revision = "021_teacher_zoom_oauth"
down_revision = "020_teaching_capability"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("teacher_zoom_connections",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("organization_id", sa.Uuid(), nullable=False), sa.Column("member_id", sa.Uuid(), nullable=False),
        *[sa.Column(name, sa.String(255)) for name in ("zoom_user_id", "zoom_account_id", "email", "display_name")],
        sa.Column("status", sa.String(40), nullable=False), sa.Column("mode", sa.String(20), nullable=False), sa.Column("tokens_enc", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True)), sa.Column("scopes", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("token_version", sa.Integer(), nullable=False), sa.Column("authorized_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "member_id", name="uq_zoom_member"), sa.UniqueConstraint("organization_id", "id", name="uq_zoom_connection_tenant"),
        sa.ForeignKeyConstraint(["organization_id", "member_id"], ["organization_members.organization_id", "organization_members.id"], name="fk_zoom_member_tenant"))
    op.create_index("ix_teacher_zoom_connections_organization_id", "teacher_zoom_connections", ["organization_id"])
    op.create_index("uq_zoom_active_identity", "teacher_zoom_connections", ["zoom_user_id"], unique=True, postgresql_where=sa.text("status IN ('ACTIVE', 'SIMULATED')"))
    op.create_table("zoom_oauth_attempts", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("state_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False), sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False), sa.Column("intended_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False), sa.Column("mode", sa.String(20), nullable=False), sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("consumed_at", sa.DateTime(timezone=True)), sa.Column("candidate_enc", sa.Text()),
        sa.ForeignKeyConstraint(["organization_id", "member_id"], ["organization_members.organization_id", "organization_members.id"], name="fk_zoom_attempt_member_tenant"))
    op.create_table("zoom_webhook_receipts", sa.Column("digest", sa.String(64), primary_key=True), sa.Column("received_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("zoom_revocations", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("zoom_user_id", sa.String(255)), sa.Column("token_enc", sa.Text()), sa.Column("mode", sa.String(20), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.add_column("organization_settings", sa.Column("default_zoom_host_member_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_default_zoom_host", "organization_settings", "organization_members", ["organization_id", "default_zoom_host_member_id"], ["organization_id", "id"])
    op.add_column("organization_settings", sa.Column("zoom_fallback_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("organization_settings", sa.Column("zoom_settings_version", sa.Integer(), nullable=False, server_default="1"))
    for col in (
        sa.Column("zoom_connection_id", sa.Uuid(), nullable=True),
        sa.Column("zoom_connection_version", sa.Integer(), nullable=True),
        sa.Column("zoom_host_member_id", sa.Uuid(), nullable=True),
        sa.Column("zoom_host_email", sa.String(255), nullable=True),
        sa.Column("zoom_provisioning_status", sa.String(40), nullable=False, server_default="NOT_PROVISIONED"),
        sa.Column("zoom_operation_key", sa.String(100), nullable=True),
        sa.Column("zoom_settings_snapshot", sa.JSON(), nullable=True),
    ):
        op.add_column("class_occurrences", col)
    op.create_foreign_key("fk_occ_zoom_connection", "class_occurrences", "teacher_zoom_connections", ["organization_id", "zoom_connection_id"], ["organization_id", "id"])
    op.create_foreign_key("fk_occ_zoom_host", "class_occurrences", "organization_members", ["organization_id", "zoom_host_member_id"], ["organization_id", "id"])
    # Legacy credentials are deliberately not transformed or copied into OAuth records.
    op.alter_column("organization_settings", "zoom_auto_meeting_enabled", server_default=sa.false())
    op.execute("UPDATE organization_integrations SET is_active = false WHERE provider = 'ZOOM'")
    op.execute("UPDATE organization_settings SET zoom_connected = false, zoom_auto_meeting_enabled = false")
    op.execute("UPDATE class_occurrences SET zoom_provisioning_status = 'LEGACY_REAUTHORIZATION_REQUIRED' WHERE meeting_provider = 'ZOOM'")


def downgrade():
    op.drop_constraint("fk_occ_zoom_host", "class_occurrences", type_="foreignkey")
    op.drop_constraint("fk_occ_zoom_connection", "class_occurrences", type_="foreignkey")
    for name in ("zoom_connection_id", "zoom_connection_version", "zoom_host_member_id", "zoom_host_email", "zoom_provisioning_status", "zoom_operation_key", "zoom_settings_snapshot"):
        op.drop_column("class_occurrences", name)
    op.drop_constraint("fk_default_zoom_host", "organization_settings", type_="foreignkey")
    for name in ("default_zoom_host_member_id", "zoom_fallback_enabled", "zoom_settings_version"):
        op.drop_column("organization_settings", name)
    for name in ("zoom_revocations", "zoom_webhook_receipts", "zoom_oauth_attempts", "teacher_zoom_connections"):
        op.drop_table(name)
