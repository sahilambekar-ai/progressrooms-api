"""Concurrent authentication controls against isolated PostgreSQL schemas."""
import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Base
from app.models.user import User
from app.core.security import get_password_hash, decode_access_token
from app.modules.auth.rate_limits import consume
from app.modules.auth.sessions import revoke_sessions
from app.modules.auth.service import AuthService

URL = os.environ.get('BILLING_TEST_POSTGRES_URL')
pytestmark = pytest.mark.skipif(not URL, reason='Set BILLING_TEST_POSTGRES_URL to isolated PostgreSQL')


@pytest_asyncio.fixture
async def auth_pg():
    schema = 'auth_test_' + uuid.uuid4().hex
    root = create_async_engine(URL, poolclass=NullPool)
    engine = create_async_engine(URL, poolclass=NullPool, connect_args={'server_settings': {'search_path': schema}})
    async with root.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with factory() as db:
            user = User(email='security@example.com', full_name='Security', role='STUDENT',
                        password_hash=get_password_hash('original-password'), is_verified=True)
            db.add(user)
            await db.commit()
            user_id = user.id
        yield factory, user_id
    finally:
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await root.dispose()


@pytest.mark.asyncio
async def test_quota_upserts_cannot_overshoot_under_concurrency(auth_pg):
    factory, _ = auth_pg
    async def attempt():
        async with factory() as db:
            try:
                await consume(db, [('probe', 'shared', 5, 900)])
                return True
            except HTTPException as exc:
                assert exc.status_code == 429
                return False
    results = await asyncio.wait_for(asyncio.gather(*(attempt() for _ in range(20))), timeout=15)
    assert sum(results) == 5


@pytest.mark.asyncio
async def test_concurrent_revocations_do_not_lose_increments(auth_pg):
    factory, user_id = auth_pg
    async def revoke():
        async with factory() as db:
            user = await db.get(User, user_id)
            await revoke_sessions(db, user)
            await db.commit()
    await asyncio.wait_for(asyncio.gather(*(revoke() for _ in range(5))), timeout=15)
    async with factory() as db:
        assert (await db.get(User, user_id)).session_version == 5


@pytest.mark.asyncio
async def test_reset_code_can_be_consumed_only_once_concurrently(auth_pg, monkeypatch):
    from app.modules.auth import service
    monkeypatch.setattr(service, 'send_otp_email', AsyncMock(return_value=True))
    factory, user_id = auth_pg
    async with factory() as db:
        code, *_ = await AuthService.forgot_password(db, 'security@example.com')
    async def reset(password):
        async with factory() as db:
            try:
                await AuthService.reset_password(db, 'security@example.com', code, password)
                return True
            except HTTPException as exc:
                assert exc.status_code == 400
                return False
    results = await asyncio.wait_for(asyncio.gather(reset('replacement-one'), reset('replacement-two')), timeout=15)
    assert sum(results) == 1
    async with factory() as db:
        assert (await db.get(User, user_id)).session_version == 1


@pytest.mark.asyncio
async def test_login_racing_reset_never_restores_pre_reset_session(auth_pg, monkeypatch):
    from app.modules.auth import service
    monkeypatch.setattr(service, 'send_otp_email', AsyncMock(return_value=True))
    factory, user_id = auth_pg
    async with factory() as db:
        code, *_ = await AuthService.forgot_password(db, 'security@example.com')
    async def old_login():
        async with factory() as db:
            try:
                return (await AuthService.login_with_password(db, 'security@example.com', 'original-password'))['access_token']
            except HTTPException as exc:
                assert exc.status_code == 401
                return None
    async def reset():
        async with factory() as db:
            await AuthService.reset_password(db, 'security@example.com', code, 'replacement-password')
    token, _ = await asyncio.wait_for(asyncio.gather(old_login(), reset()), timeout=15)
    async with factory() as db:
        version = (await db.get(User, user_id)).session_version
        assert version == 1
        assert token is None or decode_access_token(token)['sv'] < version


@pytest.mark.asyncio
async def test_migration_preserves_existing_hash_and_refuses_unsafe_rollback():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    schema = 'auth_migration_' + uuid.uuid4().hex
    root = create_async_engine(URL, poolclass=NullPool)
    engine = create_async_engine(URL, poolclass=NullPool, connect_args={'server_settings': {'search_path': schema}})
    source = Path(__file__).parents[1] / 'alembic/versions/033_auth_security.py'
    spec = importlib.util.spec_from_file_location('auth_migration', source)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with root.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with engine.begin() as conn:
            await conn.execute(text('CREATE TABLE users (id integer PRIMARY KEY, password_hash varchar(255))'))
            await conn.execute(text("INSERT INTO users VALUES (1, 'existing-bcrypt-hash')"))
            def upgrade(sync):
                migration.op = Operations(MigrationContext.configure(sync))
                migration.upgrade()
            await conn.run_sync(upgrade)
            assert (await conn.execute(text('SELECT session_version,password_hash FROM users'))).one() == (0, 'existing-bcrypt-hash')
            assert await conn.scalar(text('SELECT count(*) FROM auth_rate_limits')) == 0
            with pytest.raises(RuntimeError, match='cannot be safely downgraded'):
                migration.downgrade()
            assert await conn.scalar(text('SELECT session_version FROM users')) == 0
    finally:
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await root.dispose()
