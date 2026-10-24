"""Separate teaching eligibility from studio permissions without new memberships."""
from alembic import op
import sqlalchemy as sa
import secrets

revision = "020_teaching_capability"
down_revision = "019_class_packages"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("organization_members", sa.Column("can_teach", sa.Boolean(), nullable=False, server_default=sa.false()))
    bind = op.get_bind()
    members = sa.table("organization_members", sa.column("id", sa.Uuid()), sa.column("role", sa.String()),
                       sa.column("can_teach", sa.Boolean()), sa.column("teacher_code", sa.String()))
    bind.execute(members.update().where(members.c.role.in_(["STUDIO", "TEACHER"])).values(can_teach=True))
    op.drop_constraint("ck_organization_members_teacher_code", "organization_members", type_="check")
    used = set(bind.execute(sa.select(members.c.teacher_code).where(members.c.teacher_code.is_not(None))).scalars())
    for member_id in bind.execute(sa.select(members.c.id).where(members.c.can_teach.is_(True), members.c.teacher_code.is_(None))).scalars():
        code = str(secrets.randbelow(90_000_000) + 10_000_000)
        while code in used:
            code = str(secrets.randbelow(90_000_000) + 10_000_000)
        used.add(code)
        bind.execute(members.update().where(members.c.id == member_id).values(teacher_code=code))
    op.create_check_constraint("ck_organization_members_teacher_code", "organization_members",
                               "(NOT can_teach AND teacher_code IS NULL) OR (can_teach AND teacher_code IS NOT NULL AND teacher_code ~ '^[0-9]{8}$')")
    op.alter_column("organization_members", "can_teach", server_default=None)


def downgrade():
    op.drop_constraint("ck_organization_members_teacher_code", "organization_members", type_="check")
    op.execute("UPDATE organization_members SET teacher_code = NULL WHERE role <> 'TEACHER'")
    op.create_check_constraint("ck_organization_members_teacher_code", "organization_members",
                               "(role <> 'TEACHER' AND teacher_code IS NULL) OR (role = 'TEACHER' AND teacher_code ~ '^[0-9]{8}$')")
    op.drop_column("organization_members", "can_teach")
