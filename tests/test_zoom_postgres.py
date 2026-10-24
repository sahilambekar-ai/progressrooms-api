"""Optional PostgreSQL concurrency tests; each run uses a new disposable schema."""
import asyncio
import os
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from cryptography.fernet import Fernet
from fastapi import HTTPException
from app.core.config import settings
from app.models import Base
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.models.user import User
from app.models.session import ClassType, Session
from app.models.schedule import ClassOccurrence
from app.models.zoom import TeacherZoomConnection
from app.modules.zoom.provider import ZoomProvider, seal
from app.modules.zoom.service import now, access_token
from app.modules.zoom.meetings import meeting_access

URL = os.environ.get('ZOOM_TEST_POSTGRES_URL')
pytestmark = pytest.mark.skipif(not URL, reason='Set ZOOM_TEST_POSTGRES_URL to an isolated PostgreSQL test instance')

@pytest_asyncio.fixture
async def pg(monkeypatch):
    for name, value in {'APP_ENV': 'test', 'ZOOM_PROVIDER_MODE': 'mock', 'ZOOM_TOKEN_ENCRYPTION_KEY': Fernet.generate_key().decode()}.items():
        monkeypatch.setattr(settings, name, value)
    schema = 'zoom_test_' + uuid.uuid4().hex
    root = create_async_engine(URL, poolclass=NullPool)
    async with root.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_async_engine(URL, poolclass=NullPool, connect_args={'server_settings': {'search_path': schema}})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        owner = User(email=f'{uuid.uuid4()}@example.com', full_name='Owner', role='STUDIO', account_status='ACTIVE')
        org = Organization(name='Concurrent Zoom', slug=str(uuid.uuid4()))
        db.add_all([owner, org]); await db.flush()
        member = OrganizationMember(organization_id=org.id, user_id=owner.id, role='STUDIO', teacher_code='90000001')
        db.add(member); await db.flush()
        connection = TeacherZoomConnection(organization_id=org.id, member_id=member.id, zoom_user_id='concurrent-host',
            zoom_account_id='concurrent-account', email='host@example.invalid', mode='mock', status='SIMULATED', version=1, token_version=1,
            tokens_enc=seal(ZoomProvider().mock_tokens('concurrent-host')), expires_at=now()-timedelta(minutes=1))
        prefs = OrganizationSetting(organization_id=org.id, default_zoom_host_member_id=member.id, zoom_auto_meeting_enabled=True)
        ct = ClassType(organization_id=org.id, name='Yoga', slug='yoga')
        db.add_all([connection, prefs, ct]); await db.flush()
        sess = Session(organization_id=org.id, class_type_id=ct.id, name='Concurrent class', slug='concurrent')
        db.add(sess); await db.flush()
        start = now()+timedelta(days=1)
        occurrence = ClassOccurrence(organization_id=org.id, session_id=sess.id, instructor_id=member.id, scheduled_start_at=start,
            scheduled_end_at=start+timedelta(minutes=45), actual_start_at=start, actual_end_at=start+timedelta(minutes=45), status='SCHEDULED')
        db.add(occurrence); await db.commit()
        ids = org.id, connection.id, occurrence.id
    yield factory, ids
    await engine.dispose()
    async with root.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
    await root.dispose()


@pytest.mark.asyncio
async def test_concurrent_refresh_rotates_once(pg, monkeypatch):
    factory, (_, connection_id, _) = pg
    original = ZoomProvider.refresh
    async def delayed(self, tokens):
        await asyncio.sleep(0.05)
        return await original(self, tokens)
    refresh = AsyncMock(side_effect=delayed)
    monkeypatch.setattr(ZoomProvider, 'refresh', lambda self, tokens: refresh(self, tokens))
    async def get_token():
        async with factory() as db:
            connection = await db.get(TeacherZoomConnection, connection_id)
            token = await access_token(db, connection)
            await db.commit()
            return token
    first, second = await asyncio.gather(get_token(), get_token())
    assert first == second and refresh.await_count == 1


@pytest.mark.asyncio
async def test_simultaneous_provisioning_never_duplicates(pg, monkeypatch):
    factory, (org_id, _, occurrence_id) = pg
    original = ZoomProvider.create
    calls = []
    async def delayed(self, token, payload, operation):
        calls.append(operation)
        await asyncio.sleep(0.05)
        return await original(self, token, payload, operation)
    monkeypatch.setattr(ZoomProvider, 'create', delayed)
    async def provision():
        async with factory() as db:
            try:
                org = await db.get(Organization, org_id)
                occ = await db.get(ClassOccurrence, occurrence_id)
                return await meeting_access(db, occ, org)
            except HTTPException as exc:
                await db.rollback()
                return exc.status_code
    outcomes = await asyncio.gather(provision(), provision())
    assert len(calls) == 1
    ids = [result['meeting_id'] for result in outcomes if isinstance(result, dict)]
    assert ids and len(set(ids)) == 1
    retry = await provision()
    assert retry['meeting_id'] == ids[0] and len(calls) == 1


@pytest.mark.asyncio
async def test_database_default_host_cannot_cross_tenants(pg):
    from sqlalchemy import update, select
    from sqlalchemy.exc import IntegrityError
    factory, (org_id, _, _) = pg
    async with factory() as db:
        user = User(email=f'{uuid.uuid4()}@example.com', full_name='Other owner', role='STUDIO')
        org = Organization(name='Other tenant', slug=str(uuid.uuid4()))
        db.add_all([user, org]); await db.flush()
        member = OrganizationMember(organization_id=org.id, user_id=user.id, role='STUDIO', teacher_code='90000002')
        db.add(member); await db.commit()
        with pytest.raises(IntegrityError):
            await db.execute(update(OrganizationSetting).where(OrganizationSetting.organization_id == org_id).values(default_zoom_host_member_id=member.id))
            await db.commit()
        await db.rollback()
