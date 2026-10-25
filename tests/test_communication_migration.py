"""Exercise the deployed schema path instead of creating communication models directly."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.models import Base
from app.models import communication
from app.models.plan import Plan
from app.validation_seed import seed


@pytest.mark.asyncio
async def test_migration_creates_communication_schema_and_seed_works(monkeypatch):
    source = Path(__file__).parents[1] / "alembic/versions/031_communication_schema.py"
    spec = importlib.util.spec_from_file_location("communication_migration", source)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    names = {model.__tablename__ for model in vars(communication).values()
             if isinstance(model, type) and model.__module__ == communication.__name__
             and hasattr(model, "__tablename__")}
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            def upgrade(sync):
                Base.metadata.create_all(sync, tables=[table for table in Base.metadata.sorted_tables
                                                      if table.name not in names])
                monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
                migration.upgrade()
                inspector = sa.inspect(sync)
                for name in names:
                    assert {c["name"] for c in inspector.get_columns(name)} == set(Base.metadata.tables[name].columns.keys())
                    actual = {(tuple(i["column_names"]), bool(i["unique"])) for i in inspector.get_indexes(name)}
                    expected = {(tuple(c.name for c in i.columns), bool(i.unique)) for i in Base.metadata.tables[name].indexes}
                    assert actual == expected
            await connection.run_sync(upgrade)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.add_all([Plan(slug=slug, name=slug.title(), price=0) for slug in ("core", "studio")])
            await session.commit()
            result = await seed(session, "uat", "migration-test-private-secret-at-least-32-characters")
            assert result["students"] == 55
            assert await session.scalar(sa.select(sa.func.count()).select_from(communication.UserCommunicationPreference)) == 68
        async with engine.begin() as connection:
            def downgrade(sync):
                monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
                migration.downgrade()
                assert not names.intersection(sa.inspect(sync).get_table_names())
                assert sync.scalar(sa.text("SELECT count(*) FROM users")) == 68
            await connection.run_sync(downgrade)
    finally:
        await engine.dispose()
