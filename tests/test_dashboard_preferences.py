import uuid
import httpx
import pytest
from app.main import app, api_v1
from app.core.database import get_db
from app.core.security import create_access_token
from app.models.user import User
from app.models.organization import Organization, OrganizationMember


@pytest.mark.asyncio
async def test_dashboard_font_default_validation_and_user_isolation(db_session):
    users = [User(email=f"{uuid.uuid4().hex}@example.com", full_name="Font Tester", role=role,
                  is_verified=True, account_status="ACTIVE") for role in ("STUDIO", "TEACHER", "STUDENT")]
    db_session.add_all(users)
    org = Organization(name="Font Test Studio", slug=uuid.uuid4().hex)
    db_session.add(org)
    await db_session.flush()
    db_session.add(OrganizationMember(organization_id=org.id, user_id=users[0].id, role="STUDIO"))
    db_session.add(OrganizationMember(organization_id=org.id, user_id=users[1].id, role="TEACHER", can_teach=True))
    await db_session.commit()
    async def override():
        yield db_session
    api_v1.dependency_overrides[get_db] = override
    headers = lambda user: {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}
    path = "/api/v1/auth/me/preferences"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(path)).status_code == 401
            for user in users:
                assert (await client.get(path, headers=headers(user))).json() == {"dashboard_font": "ibm-plex"}
            saved = await client.patch(path, headers=headers(users[0]), json={"dashboard_font": "manrope"})
            assert saved.status_code == 200 and saved.json()["dashboard_font"] == "manrope"
            await db_session.refresh(users[0])
            assert users[0].dashboard_font == "manrope"
            assert (await client.patch(path, headers=headers(users[0]), json={"dashboard_font": "unknown"})).status_code == 422
            for user in users[1:]:
                assert (await client.get(path, headers=headers(user))).json()["dashboard_font"] == "ibm-plex"
            # A fresh session token reads the persisted choice, independent of browser storage.
            assert (await client.get(path, headers=headers(users[0]))).json()["dashboard_font"] == "manrope"
            assert (await client.patch(path, json={"dashboard_font": "lora"})).status_code == 401
    finally:
        api_v1.dependency_overrides.clear()


def test_font_migration_defaults_existing_users_and_round_trips(monkeypatch):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import create_engine, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    source = Path(__file__).parents[1] / "alembic/versions/029_dashboard_font.py"
    spec = importlib.util.spec_from_file_location("font_migration", source)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO users (id) VALUES (1)"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert connection.execute(text("SELECT dashboard_font FROM users WHERE id=1")).scalar() == "ibm-plex"
        migration.downgrade()
        assert connection.execute(text("SELECT id FROM users")).scalar() == 1
    engine.dispose()
