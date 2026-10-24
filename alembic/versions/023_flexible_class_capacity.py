"""Allow unlimited class capacity."""
from alembic import op
import sqlalchemy as sa
revision = "023_flexible_class_capacity"
down_revision = "022_teacher_credentials"
branch_labels = None
depends_on = None

def upgrade():
    op.alter_column("sessions", "capacity", existing_type=sa.Integer(), nullable=True)

def downgrade():
    op.execute("UPDATE sessions SET capacity = 20 WHERE capacity IS NULL")
    op.alter_column("sessions", "capacity", existing_type=sa.Integer(), nullable=False)
