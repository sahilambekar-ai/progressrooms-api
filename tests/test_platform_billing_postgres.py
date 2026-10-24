"""Optional real PostgreSQL tests for concurrent joins and monthly generation."""
import asyncio
import os
import uuid
from datetime import date, datetime, timezone
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from app.models import Base
from app.models.organization import Organization
from app.models.platform_billing import PlatformInvoice, StudentJoinEvent
from app.models.commerce import Order, SessionEnrollment
from app.models.schedule import ClassOccurrence
from app.models.user import User
from app.modules.billing import platform
from test_platform_billing import setup, student

URL = os.environ.get("BILLING_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="Set BILLING_TEST_POSTGRES_URL to a PostgreSQL test instance")


@pytest_asyncio.fixture
async def billing_pg():
    schema = "billing_test_" + uuid.uuid4().hex
    root = create_async_engine(URL, poolclass=NullPool)
    engine = create_async_engine(URL, poolclass=NullPool, connect_args={"server_settings": {"search_path": schema}})
    async with root.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            org, _, _, _, session, occ = await setup(db)
            user, enrollment = await student(db, org, session)
            await db.commit()
            ids = org.id, user.id, enrollment.id, occ.id
        yield factory, ids
    finally:
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await root.dispose()


@pytest.mark.asyncio
async def test_concurrent_joins_count_student_once(billing_pg):
    factory, (org_id, user_id, enrollment_id, occ_id) = billing_pg
    async def click():
        async with factory() as db:
            await platform.record_join(db, await db.get(Organization, org_id), await db.get(User, user_id),
                await db.get(SessionEnrollment, enrollment_id), await db.get(ClassOccurrence, occ_id))
            await db.commit()
    await asyncio.gather(*(click() for _ in range(5)))
    async with factory() as db:
        data = await platform.dashboard(db, await db.get(Organization, org_id))
        assert data["active_students"] == 1 and data["join_clicks"] == 5
        assert data["platform_amount"] == 0


@pytest.mark.asyncio
async def test_concurrent_monthly_generation_creates_one_statement(billing_pg):
    factory, (org_id, user_id, enrollment_id, occ_id) = billing_pg
    async with factory() as db:
        db.add(StudentJoinEvent(pricing_region="INDIA", organization_id=org_id, user_id=user_id, enrollment_id=enrollment_id,
            occurrence_id=occ_id, month=date(2026, 1, 1), clicked_at=datetime(2026, 1, 15, tzinfo=timezone.utc)))
        await db.commit()
    async def generate():
        async with factory() as db:
            await platform.dashboard(db, await db.get(Organization, org_id))
            await db.commit()
    await asyncio.gather(generate(), generate(), generate())
    async with factory() as db:
        assert await db.scalar(select(func.count(PlatformInvoice.id)).where(PlatformInvoice.organization_id == org_id)) == 1


@pytest.mark.asyncio
async def test_shared_pass_joins_and_reporting_do_not_deadlock(billing_pg):
    from app.modules.student.router import get_class_meeting_url
    factory, (org_id, user_id, enrollment_id, occ_id) = billing_pg
    async with factory() as db:
        order = Order(organization_id=org_id, user_id=user_id, order_number=str(uuid.uuid4()), total_amount=500, status="PAID")
        db.add(order); await db.flush()
        enrollment = await db.get(SessionEnrollment, enrollment_id)
        enrollment.purchase_order_id = order.id
        enrollment.total_sessions = 10
        await db.commit()
    async def join():
        async with factory() as db:
            return await get_class_meeting_url(occ_id, await db.get(User, user_id), db)
    async def report():
        async with factory() as db:
            data = await platform.dashboard(db, await db.get(Organization, org_id))
            await db.commit()
            return data
    results = await asyncio.wait_for(asyncio.gather(join(), report(), join(), report()), timeout=10)
    assert results[0]["meeting_url"] == "https://example.com/join"
    assert results[2]["meeting_url"] == "https://example.com/join"
