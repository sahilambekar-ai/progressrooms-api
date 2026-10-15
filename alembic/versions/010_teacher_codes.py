"""Add immutable unique eight-digit teacher codes."""
from alembic import op
import sqlalchemy as sa

revision = "010_teacher_codes"
down_revision = "009_enrollment_billing_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("organization_members", sa.Column("teacher_code", sa.String(8), nullable=True))
    op.execute("""
        WITH teachers AS (
            SELECT id, row_number() OVER (ORDER BY created_at, id) AS sequence
            FROM organization_members
            WHERE role = 'TEACHER'
        )
        UPDATE organization_members AS member
        SET teacher_code = lpad((10000000 + teachers.sequence)::text, 8, '0')
        FROM teachers
        WHERE member.id = teachers.id
    """)
    op.create_index("ix_organization_members_teacher_code", "organization_members", ["teacher_code"], unique=True)
    op.create_check_constraint(
        "ck_organization_members_teacher_code",
        "organization_members",
        "(role <> 'TEACHER' AND teacher_code IS NULL) OR (role = 'TEACHER' AND teacher_code ~ '^[0-9]{8}$')",
    )


def downgrade():
    op.drop_constraint("ck_organization_members_teacher_code", "organization_members", type_="check")
    op.drop_index("ix_organization_members_teacher_code", table_name="organization_members")
    op.drop_column("organization_members", "teacher_code")
