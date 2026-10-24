"""Disposable-schema PostgreSQL checks for money-affecting concurrency."""
import asyncio
import importlib.util
import uuid
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, func
from app.models.plan import Plan, OrganizationSubscription
from app.models.organization import OrganizationMember
from app.models.saas_billing import SaaSCheckout
from app.modules.billing import saas
from test_platform_billing_postgres import billing_pg, URL
from test_saas_billing import billing_config

pytestmark = pytest.mark.skipif(not URL, reason="Set BILLING_TEST_POSTGRES_URL to a PostgreSQL test instance")


@pytest.mark.asyncio
async def test_concurrent_checkout_and_fulfillment(billing_pg, monkeypatch):
    factory, (org_id, _, _, _) = billing_pg
    async with factory() as db:
        plan = Plan(name="Studio", slug="studio", price=3999)
        db.add(plan); await db.flush()
        owner_id = await db.scalar(select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.role == "STUDIO"))
        await saas.initialize_subscription(db, org_id, "studio", verified=True)
        await db.commit()
        plan_id = plan.id
    order_id = f"order_{uuid.uuid4().hex}"
    provider = AsyncMock(return_value={"id": order_id, "amount": 471882, "currency": "INR"})
    monkeypatch.setattr(saas, "provider_request", provider)
    async def create():
        async with factory() as db:
            row = await saas.create_checkout(db, org_id, owner_id, plan_id, "IN", uuid.uuid4().hex, 471882)
            row_id = row.id
            await db.commit()
            return row_id
    ids = await asyncio.gather(create(), create(), create())
    assert len(set(ids)) == 1 and provider.await_count == 1
    payment = {"id": f"pay_{uuid.uuid4().hex}", "order_id": order_id, "amount": 471882,
               "currency": "INR", "status": "captured"}
    async def fulfill():
        async with factory() as db:
            row = await db.get(SaaSCheckout, ids[0])
            await saas.fulfill(db, row, payment)
            await db.commit()
            return row.service_ends_at
    ends = await asyncio.gather(fulfill(), fulfill(), fulfill())
    assert len(set(ends)) == 1
    async with factory() as db:
        assert await db.scalar(select(func.count(SaaSCheckout.id))) == 1
        assert (await db.scalar(select(OrganizationSubscription))).ends_at == ends[0]


@pytest.mark.asyncio
async def test_migrations_round_trip_in_disposable_schema(billing_pg):
    factory, _ = billing_pg
    async with factory() as db:
        connection = await db.connection()
        def check(conn):
            from alembic.migration import MigrationContext
            from alembic.operations import Operations
            modules = []
            for name in ["027_saas_checkout", "028_usage_pricing_region"]:
                spec = importlib.util.spec_from_file_location(name, Path("alembic/versions") / f"{name}.py")
                module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); modules.append(module)
            with Operations.context(MigrationContext.configure(conn)):
                for module in reversed(modules):
                    module.downgrade()
                for module in modules:
                    module.upgrade()
            assert conn.scalar(select(func.count(Plan.id))) == 3
        await connection.run_sync(check)
        await db.rollback()
