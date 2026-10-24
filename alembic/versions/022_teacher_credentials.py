"""Studio-managed teacher credentials."""
from alembic import op
import sqlalchemy as sa
revision = "022_teacher_credentials"
down_revision = "021_teacher_zoom_oauth"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("zoom_oauth_attempts", sa.Column("expected_email", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("managed_password_enc", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("teacher_invitation_fingerprint", sa.String(64), nullable=True))

def downgrade():
    op.drop_column("zoom_oauth_attempts", "expected_email")
    op.drop_column("users", "teacher_invitation_fingerprint")
    op.drop_column("users", "managed_password_enc")
