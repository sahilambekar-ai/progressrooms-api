"""Exercise every registered authenticated route without credentials or a database."""
import re
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi.routing import APIRoute, iter_route_contexts

from app.core.database import get_db
from app.main import app, api_v1
from app.modules.auth.dependencies import get_current_user


def requires_user(dependency):
    return dependency.call is get_current_user or any(requires_user(child) for child in dependency.dependencies)


CASES = [(method, route.path) for route in iter_route_contexts(api_v1.routes)
         if isinstance(route.original_route, APIRoute) and requires_user(route.dependant)
         for method in sorted(route.methods)]


def test_authenticated_route_inventory_is_not_empty():
    # A router representation change must not silently remove the audit checks.
    assert len(CASES) >= 127


@pytest_asyncio.fixture
async def anonymous_client():
    async def no_database():
        # Any attempted database operation will fail instead of reaching real data.
        yield object()
    previous = api_v1.dependency_overrides.copy()
    api_v1.dependency_overrides[get_db] = no_database
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://audit/api/v1") as client:
            yield client
    finally:
        api_v1.dependency_overrides.clear()
        api_v1.dependency_overrides.update(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", CASES, ids=[f"{method} {path}" for method, path in CASES])
async def test_registered_authenticated_route_rejects_anonymous(anonymous_client, method, path):
    path = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
    response = await anonymous_client.request(method, path)
    assert response.status_code == 401, response.text
