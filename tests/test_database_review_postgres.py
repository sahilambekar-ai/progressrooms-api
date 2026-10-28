"""Migrations, constraints and data-preserving rollback against disposable schemas.

DATABASE_REVIEW_POSTGRES_URL must name a dedicated test database. No application
schema is reused; each test owns a randomly named schema and removes it afterward.
"""
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.models import Base

URL = os.environ.get('DATABASE_REVIEW_POSTGRES_URL')
pytestmark = pytest.mark.skipif(not URL, reason='Set DATABASE_REVIEW_POSTGRES_URL to isolated PostgreSQL')


@pytest.fixture
def migrated_pg():
    url = URL.replace('postgresql+asyncpg:', 'postgresql:')
    schema = 'review_' + uuid.uuid4().hex
    root = sa.create_engine(url)
    with root.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = sa.create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        with engine.connect() as connection:
            config = Config(str(Path(__file__).parents[1] / 'alembic.ini'))
            config.attributes['connection'] = connection
            yield connection, config
    finally:
        engine.dispose()
        with root.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        root.dispose()


def test_fresh_head_has_no_model_drift(migrated_pg):
    connection, config = migrated_pg
    command.upgrade(config, 'head')
    differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert differences == []
    assert connection.scalar(sa.text('SELECT version_num FROM alembic_version')) == '034_support_indexes'
    invalid = connection.scalar(sa.text('''SELECT count(*) FROM pg_index i
        JOIN pg_class t ON t.oid=i.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND NOT i.indisvalid'''))
    assert invalid == 0
    assert connection.scalar(sa.text('''SELECT count(*) FROM pg_constraint c
        JOIN pg_namespace n ON n.oid=c.connamespace
        WHERE n.nspname=current_schema() AND NOT c.convalidated''')) == 0


def _support_fixture(connection):
    ids = {key: str(uuid.uuid4()) for key in ['user', 'org', 'ticket', 'message']}
    connection.execute(sa.text("""INSERT INTO users (id,email,full_name,role,is_active,is_superadmin,is_verified,account_status,created_at,updated_at)
        VALUES (:user,'review@example.test','Review','STUDENT',true,false,true,'ACTIVE',now(),now())"""), ids)
    connection.execute(sa.text("""INSERT INTO organizations (id,name,slug,status,timezone,currency,created_at,updated_at)
        VALUES (:org,'Review','review','ACTIVE','Asia/Kolkata','INR',now(),now())"""), ids)
    connection.execute(sa.text("""INSERT INTO support_tickets (id,organization_id,student_id,student_name,student_email,subject,created_at,updated_at)
        VALUES (:ticket,:org,:user,'Review','review@example.test','Keep this ticket',now(),now())"""), ids)
    connection.execute(sa.text("""INSERT INTO support_messages (id,ticket_id,sender_user_id,sender_name,sender_role,message,created_at,updated_at)
        VALUES (:message,:ticket,:user,'Review','STUDENT','Keep this message',now(),now())"""), ids)
    connection.commit()
    return ids


def test_index_upgrade_and_downgrade_preserve_populated_data(migrated_pg):
    connection, config = migrated_pg
    command.upgrade(config, '033_auth_security')
    ids = _support_fixture(connection)
    command.upgrade(config, 'head')
    indexes = {i['name'] for i in sa.inspect(connection).get_indexes('support_tickets')}
    assert {'ix_support_tickets_organization_id','ix_support_tickets_student_id'} <= indexes
    assert 'ix_support_tickets_org_id' not in indexes
    connection.commit()
    command.downgrade(config, '033_auth_security')
    indexes = {i['name'] for i in sa.inspect(connection).get_indexes('support_tickets')}
    assert 'ix_support_tickets_org_id' in indexes
    assert 'ix_support_tickets_student_id' not in indexes
    assert connection.scalar(sa.text('SELECT subject FROM support_tickets WHERE id=:ticket'), ids) == 'Keep this ticket'
    assert connection.scalar(sa.text('SELECT message FROM support_messages WHERE id=:message'), ids) == 'Keep this message'
    connection.commit()
    command.upgrade(config, 'head')
    assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_auth_downgrade_is_blocked_atomically(migrated_pg):
    connection, config = migrated_pg
    command.upgrade(config, 'head')
    ids = _support_fixture(connection)
    with pytest.raises(RuntimeError, match='cannot be safely downgraded'):
        command.downgrade(config, '032_zoom_meeting_mail')
    connection.rollback()
    # 034's reversal must roll back too when 033 refuses the same transaction.
    assert connection.scalar(sa.text('SELECT version_num FROM alembic_version')) == '034_support_indexes'
    assert connection.scalar(sa.text('SELECT subject FROM support_tickets WHERE id=:ticket'), ids) == 'Keep this ticket'
    assert 'ix_support_tickets_student_id' in {i['name'] for i in sa.inspect(connection).get_indexes('support_tickets')}


def test_foreign_keys_unique_constraints_and_cascades(migrated_pg):
    connection, config = migrated_pg
    command.upgrade(config, 'head')
    ids = _support_fixture(connection)
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            connection.execute(sa.text("UPDATE support_tickets SET student_id=:unknown WHERE id=:ticket"), {**ids, 'unknown': str(uuid.uuid4())})
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            connection.execute(sa.text("INSERT INTO users (id,email,full_name,role,is_active,is_superadmin,is_verified,account_status,created_at,updated_at) VALUES (:other,'review@example.test','Duplicate','STUDENT',true,false,true,'ACTIVE',now(),now())"), {'other':str(uuid.uuid4())})
    connection.execute(sa.text('DELETE FROM users WHERE id=:user'), ids)
    assert connection.scalar(sa.text('SELECT student_id FROM support_tickets WHERE id=:ticket'), ids) is None
    connection.execute(sa.text('DELETE FROM organizations WHERE id=:org'), ids)
    assert connection.scalar(sa.text('SELECT count(*) FROM support_tickets')) == 0
    assert connection.scalar(sa.text('SELECT count(*) FROM support_messages')) == 0


def test_backup_script_restores_complete_database(migrated_pg, tmp_path):
    """Use the deployment backup script, real PG tools, and a fresh restore DB."""
    import hashlib
    import shutil
    import subprocess
    from app.core.security import get_password_hash, verify_password

    connection, config = migrated_pg
    command.upgrade(config, 'head')
    ids = _support_fixture(connection)
    encoded = get_password_hash('restore-safe-password')
    connection.execute(sa.text('UPDATE users SET password_hash=:hash,session_version=7 WHERE id=:user'), {**ids, 'hash':encoded})
    order_id = uuid.uuid4()
    connection.execute(Base.metadata.tables['orders'].insert().values(id=order_id, organization_id=uuid.UUID(ids['org']), user_id=uuid.UUID(ids['user']), order_number='RESTORE-001', total_amount=125.50, currency='INR', status='PAID'))
    connection.execute(Base.metadata.tables['payments'].insert().values(order_id=order_id, amount=125.50, currency='INR', status='SUCCESS', idempotency_key='restore-once', provider_payment_id='restore-payment'))
    connection.execute(Base.metadata.tables['payment_webhook_events'].insert().values(event_id='restore-event', event_type='payment.captured', payload={'nested': {'amount':12550}, 'labels':['synthetic','restore']}, is_processed=True))
    connection.execute(Base.metadata.tables['auth_rate_limits'].insert().values(key='a'*64,window_start=1,count=3,expires_at=2000000000))
    connection.commit()
    url = sa.engine.make_url(URL.replace('postgresql+asyncpg:', 'postgresql:'))
    bindir = os.environ.get('DATABASE_REVIEW_PG_BIN', '')
    pg_restore = str(Path(bindir) / 'pg_restore') if bindir else shutil.which('pg_restore')
    assert pg_restore, 'Install PostgreSQL client tools for the restore drill'
    env = {**os.environ, 'PGHOST':url.host or '', 'PGPORT':str(url.port or 5432), 'PGUSER':url.username or '', 'PGDATABASE':url.database or '', 'PGPASSWORD':url.password or '', 'POSTGRES_USER':url.username or '', 'POSTGRES_DB':url.database or ''}
    # Do not let a developer's search_path/service settings narrow or redirect a dump.
    env.pop('PGOPTIONS', None)
    env.pop('PGSERVICE', None)
    if bindir:
        env['PATH'] = bindir + os.pathsep + env['PATH']
    wrapper = tmp_path / 'compose-local'
    wrapper.write_text('#!/usr/bin/env bash\nset -euo pipefail\n[[ "$1" == exec && "$2" == -T && "$3" == postgres ]]\nshift 3\nexec "$@"\n')
    wrapper.chmod(0o700)
    archive = tmp_path / 'database.dump'
    script = Path(__file__).parents[1] / 'deploy/backup-db.sh'

    def snapshot(conn):
        result = {}
        inspector = sa.inspect(conn)
        for schema in inspector.get_schema_names():
            if schema != connection.dialect.default_schema_name:
                continue
            for table in inspector.get_table_names(schema=schema):
                relation = f'"{schema}"."{table}"'
                result[f'{schema}.{table}'] = tuple(conn.execute(sa.text(f"SELECT count(*),md5(coalesce(string_agg(row_to_json(t)::text,E'\\n' ORDER BY row_to_json(t)::text),'')) FROM {relation} t")).one())
        return result

    expected = snapshot(connection)
    assert len(expected) == len(Base.metadata.tables) + 1  # all models plus Alembic revision
    connection.commit()
    subprocess.run(['bash', str(script), str(archive), str(wrapper)], env=env, check=True, capture_output=True)
    assert archive.stat().st_mode & 0o077 == 0
    checksum = archive.with_suffix('.dump.sha256').read_text().split()[0]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == checksum
    # Refuse accidental replacement of a known backup.
    assert subprocess.run(['bash',str(script),str(archive),str(wrapper)],env=env,capture_output=True).returncode != 0
    failed = tmp_path / 'failed.dump'
    assert subprocess.run(['bash',str(script),str(failed),'/usr/bin/false'],env=env,capture_output=True).returncode != 0
    assert not failed.exists() and not list(tmp_path.glob('failed.dump.partial.*'))

    restore_name = 'restore_' + uuid.uuid4().hex
    admin = sa.create_engine(url, isolation_level='AUTOCOMMIT')
    restored = None
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{restore_name}"'))
        restore_env = {**env, 'PGDATABASE':restore_name}
        subprocess.run([pg_restore,'--exit-on-error','--single-transaction','--no-owner','--no-acl','--dbname',restore_name,str(archive)],env=restore_env,check=True,capture_output=True)
        restored = sa.create_engine(url.set(database=restore_name), connect_args={'options':f'-csearch_path={connection.dialect.default_schema_name}'})
        with restored.connect() as conn:
            assert snapshot(conn) == expected
            assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
            row = conn.execute(sa.text('SELECT password_hash,session_version FROM users WHERE id=:user'),ids).one()
            assert verify_password('restore-safe-password',row.password_hash) and row.session_version == 7
            assert conn.scalar(sa.text("SELECT amount FROM payments WHERE idempotency_key='restore-once'")) == 125.50
    finally:
        if restored:
            restored.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{restore_name}" WITH (FORCE)'))
        admin.dispose()


def test_same_studio_teacher_and_single_admin_constraints(migrated_pg):
    connection, config = migrated_pg
    command.upgrade(config, 'head')
    ids = _support_fixture(connection)
    org2, teacher, member = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    connection.execute(Base.metadata.tables['organizations'].insert().values(id=org2,name='Other',slug='other'))
    connection.execute(Base.metadata.tables['users'].insert().values(id=teacher,email='teacher@example.test',full_name='Teacher',role='TEACHER'))
    connection.execute(Base.metadata.tables['organization_members'].insert().values(id=member,organization_id=org2,user_id=teacher,role='TEACHER',teacher_code='12345678',can_teach=True))
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            connection.execute(Base.metadata.tables['organization_students'].insert().values(organization_id=uuid.UUID(ids['org']),user_id=uuid.UUID(ids['user']),teacher_member_id=member))
    # Valid same-studio assignment remains allowed.
    connection.execute(Base.metadata.tables['organization_students'].insert().values(organization_id=org2,user_id=uuid.UUID(ids['user']),teacher_member_id=member))
    connection.execute(Base.metadata.tables['users'].insert().values(email='admin1@example.test',full_name='Admin',role='SUPER_ADMIN',is_superadmin=True))
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            connection.execute(Base.metadata.tables['users'].insert().values(email='admin2@example.test',full_name='Admin',role='SUPER_ADMIN',is_superadmin=True))


def test_index_upgrade_failure_is_atomic(migrated_pg):
    connection, config = migrated_pg
    command.upgrade(config, '033_auth_security')
    ids = _support_fixture(connection)
    # Simulate a manually-created conflicting index from a drifted installation.
    connection.execute(sa.text('CREATE INDEX ix_support_tickets_student_id ON support_tickets(student_id)'))
    connection.commit()
    with pytest.raises(sa.exc.ProgrammingError):
        command.upgrade(config, 'head')
    connection.rollback()
    assert connection.scalar(sa.text('SELECT version_num FROM alembic_version')) == '033_auth_security'
    indexes = {i['name'] for i in sa.inspect(connection).get_indexes('support_tickets')}
    assert 'ix_support_tickets_org_id' in indexes
    assert 'ix_support_tickets_organization_id' not in indexes
    assert connection.scalar(sa.text('SELECT subject FROM support_tickets WHERE id=:ticket'),ids) == 'Keep this ticket'
