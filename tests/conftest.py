import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.ext.compiler import compiles
from app.models import Base
from app.core.database import get_db
from app.main import app

# Ensure SQLite compiles PostgreSQL types cleanly for test execution
@compiles(UUID, "sqlite")
def compile_uuid_sqlite(type_, compiler, **kw):
    return "VARCHAR(36)"

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

@pytest_asyncio.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migration 027 supplies the registration catalog in deployed databases.
        import uuid
        from app.models.plan import Plan
        await conn.execute(Plan.__table__.insert(), [
            {"id": uuid.uuid4(), "slug": slug, "name": name, "price": price}
            for slug, name, price in [("starter", "Starter", 0), ("core", "Core", 1499), ("studio", "Studio", 3999)]
        ])
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()

@pytest_asyncio.fixture
async def db_session(test_engine):
    # Application commits must stay inside this test's outer transaction.
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        # SQLite otherwise delays BEGIN until a write, allowing a released
        # SAVEPOINT to commit outside the outer transaction.
        await connection.exec_driver_sql("BEGIN")
        session_factory = async_sessionmaker(
            connection, expire_on_commit=False, class_=AsyncSession,
            join_transaction_mode="create_savepoint",
        )
        try:
            async with session_factory() as session:
                yield session
        finally:
            await transaction.rollback()
