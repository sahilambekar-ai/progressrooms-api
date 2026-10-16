"""Require codes for teachers and reserve null codes for non-teachers."""
from alembic import op

revision = "011_require_teacher_codes"
down_revision = "010_teacher_codes"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_organization_members_teacher_code", "organization_members", type_="check")
    op.create_check_constraint(
        "ck_organization_members_teacher_code",
        "organization_members",
        "(role <> 'TEACHER' AND teacher_code IS NULL) OR (role = 'TEACHER' AND teacher_code ~ '^[0-9]{8}$')",
    )


def downgrade():
    op.drop_constraint("ck_organization_members_teacher_code", "organization_members", type_="check")
    op.create_check_constraint(
        "ck_organization_members_teacher_code",
        "organization_members",
        "teacher_code IS NULL OR teacher_code ~ '^[0-9]{8}$'",
    )
