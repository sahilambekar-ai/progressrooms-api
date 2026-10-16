"""Explicit account roles, singleton administrator, and studio-scoped teacher assignments.

Existing privileged accounts are preserved; ambiguous multiple administrators fail safely.
"""
from alembic import op
import sqlalchemy as sa

revision = '004_account_roles'
down_revision = '003_completion_percentage'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('role', sa.String(20), nullable=True))
    for name, size in [('username', 100), ('address', 1000), ('location', 255)]:
        op.add_column('users', sa.Column(name, sa.String(size), nullable=True))
    op.execute("""UPDATE users SET role = CASE
        WHEN is_superadmin THEN 'SUPER_ADMIN'
        WHEN EXISTS (SELECT 1 FROM organization_members m WHERE m.user_id=users.id AND m.role IN ('OWNER','ADMIN','STUDIO')) THEN 'STUDIO'
        WHEN EXISTS (SELECT 1 FROM organization_members m WHERE m.user_id=users.id AND m.role IN ('INSTRUCTOR','TEACHER')) THEN 'TEACHER'
        ELSE 'STUDENT' END""")
    op.execute("UPDATE organization_members SET role = CASE WHEN role IN ('OWNER','ADMIN') THEN 'STUDIO' WHEN role='INSTRUCTOR' THEN 'TEACHER' ELSE role END")
    op.alter_column('users', 'role', nullable=False, server_default='STUDENT')
    op.create_check_constraint('ck_users_role', 'users', "role IN ('SUPER_ADMIN','STUDIO','TEACHER','STUDENT')")
    op.create_check_constraint('ck_users_superadmin_consistent', 'users', "is_superadmin = (role = 'SUPER_ADMIN')")
    op.create_index('ix_users_role', 'users', ['role'])
    op.create_index('uq_users_single_super_admin', 'users', ['role'], unique=True, postgresql_where=sa.text("role = 'SUPER_ADMIN'"))
    op.create_unique_constraint('uq_users_username', 'users', ['username'])
    op.create_unique_constraint('uq_org_member', 'organization_members', ['organization_id', 'user_id'])
    op.create_unique_constraint('uq_org_member_identity', 'organization_members', ['organization_id', 'id'])
    op.create_unique_constraint('uq_org_student', 'organization_students', ['organization_id', 'user_id'])
    op.add_column('organization_students', sa.Column('teacher_member_id', sa.UUID(), nullable=True))
    op.create_index('ix_organization_students_teacher_member_id', 'organization_students', ['teacher_member_id'])
    op.create_foreign_key('fk_student_teacher_same_studio', 'organization_students', 'organization_members', ['organization_id','teacher_member_id'], ['organization_id','id'])


def downgrade():
    op.drop_constraint('fk_student_teacher_same_studio', 'organization_students', type_='foreignkey')
    op.drop_index('ix_organization_students_teacher_member_id', 'organization_students')
    op.drop_column('organization_students', 'teacher_member_id')
    for table, name in [('organization_students','uq_org_student'), ('organization_members','uq_org_member_identity'), ('organization_members','uq_org_member'), ('users','uq_users_username')]:
        op.drop_constraint(name, table, type_='unique')
    op.drop_index('uq_users_single_super_admin', 'users')
    op.drop_index('ix_users_role', 'users')
    op.drop_constraint('ck_users_superadmin_consistent', 'users', type_='check')
    op.drop_constraint('ck_users_role', 'users', type_='check')
    for name in ['role','username','address','location']:
        op.drop_column('users', name)
    op.execute("UPDATE organization_members SET role=CASE WHEN role='STUDIO' THEN 'OWNER' WHEN role='TEACHER' THEN 'INSTRUCTOR' ELSE role END")
