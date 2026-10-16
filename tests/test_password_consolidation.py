"""Exercise the real migration on representative legacy data, including rollback."""
import importlib.util
from pathlib import Path
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


@pytest.fixture
def legacy_db(monkeypatch):
    path = Path(__file__).parents[1] / "alembic/versions/016_consolidate_password_credentials.py"
    spec = importlib.util.spec_from_file_location("password_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    users = sa.Table("users", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("created_at", sa.DateTime()), sa.Column("updated_at", sa.DateTime()))
    legacy = sa.Table("user_auth_methods", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("auth_type", sa.String(50)), sa.Column("credential_hash", sa.String(255)))
    metadata.create_all(engine)
    with engine.begin() as conn:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        yield conn, users, legacy, migration
    engine.dispose()


def add_user(conn, users, legacy, canonical=None, hashes=(), provider="PASSWORD"):
    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    conn.execute(users.insert().values(id=user_id, password_hash=canonical, created_at=now, updated_at=now))
    for value in hashes:
        conn.execute(legacy.insert().values(id=uuid.uuid4(), user_id=user_id, auth_type=provider, credential_hash=value))
    return user_id


def test_backfill_preserves_canonical_and_downgrade_restores_passwords(legacy_db):
    conn, users, legacy, migration = legacy_db
    canonical = add_user(conn, users, legacy, "current", ["old", "older"])
    backfill = add_user(conn, users, legacy, hashes=["legacy", "legacy"])
    empty = add_user(conn, users, legacy, "", ["empty-backfill"])
    otp = add_user(conn, users, legacy, hashes=[None], provider="OTP")
    migration.upgrade()
    assert "user_auth_methods" not in sa.inspect(conn).get_table_names()
    passwords = dict(conn.execute(sa.select(users.c.id, users.c.password_hash)).all())
    assert passwords == {canonical: "current", backfill: "legacy", empty: "empty-backfill", otp: None}
    migration.downgrade()
    restored = sa.Table("user_auth_methods", sa.MetaData(), autoload_with=conn)
    rows = conn.execute(sa.select(restored.c.auth_type, restored.c.credential_hash)).all()
    assert sorted(rows) == [("PASSWORD", "current"), ("PASSWORD", "empty-backfill"), ("PASSWORD", "legacy")]


@pytest.mark.parametrize("hashes,provider", [(["a", "b"], "PASSWORD"), (["oauth-token"], "GOOGLE")])
def test_preflight_aborts_without_changing_data(legacy_db, hashes, provider):
    conn, users, legacy, migration = legacy_db
    add_user(conn, users, legacy, hashes=["valid"])
    add_user(conn, users, legacy, hashes=hashes, provider=provider)
    with pytest.raises(RuntimeError):
        migration.upgrade()
    assert "user_auth_methods" in sa.inspect(conn).get_table_names()
    assert conn.scalar(sa.select(sa.func.count()).select_from(users).where(users.c.password_hash.is_not(None))) == 0
