"""Student WhatsApp contact and resumable enrollment validity."""
from alembic import op
import sqlalchemy as sa
revision = "007_student_profiles_pause"
down_revision = "006_membership_tiers"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("users", sa.Column("whatsapp_number", sa.String(50), nullable=True))
    op.add_column("session_enrollments", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))

def downgrade():
    op.drop_column("session_enrollments", "paused_at")
    op.drop_column("users", "whatsapp_number")
