import uuid
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio

from app.core.database import get_db
from app.core.security import create_access_token
from app.main import api_v1, app
from app.models.commerce import Order, SessionEnrollment
from app.models.organization import Organization, OrganizationMember
from app.models.session import ClassType, Session, SessionInstructor, SessionPricing
from app.models.user import User


@pytest_asyncio.fixture
async def client(db_session):
    async def dependency():
        yield db_session
    api_v1.dependency_overrides[get_db] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1/") as value:
        yield value
    api_v1.dependency_overrides.clear()


def auth(user):
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}


@pytest.mark.asyncio
async def test_owner_billing_uses_subscription_values_and_teacher_filter(client, db_session):
    owner = User(email=f"owner-{uuid.uuid4()}@example.com", full_name="Owner", role="STUDIO", is_verified=True, account_status="ACTIVE")
    teacher_user = User(email=f"teacher-{uuid.uuid4()}@example.com", full_name="Asha Teacher", role="TEACHER", is_verified=True, account_status="ACTIVE")
    student = User(email=f"student-{uuid.uuid4()}@example.com", full_name="Riya Student", role="STUDENT", is_verified=True, account_status="ACTIVE")
    db_session.add_all([owner, teacher_user, student])
    await db_session.flush()
    org = Organization(name="Billing Studio", slug=uuid.uuid4().hex, currency="INR")
    db_session.add(org)
    await db_session.flush()
    db_session.add(OrganizationMember(organization_id=org.id, user_id=owner.id, role="STUDIO"))
    teacher = OrganizationMember(organization_id=org.id, user_id=teacher_user.id, role="TEACHER")
    db_session.add(teacher)
    kind = ClassType(organization_id=org.id, name="Yoga", slug=uuid.uuid4().hex)
    db_session.add(kind)
    await db_session.flush()
    session = Session(organization_id=org.id, class_type_id=kind.id, name="Monthly Yoga", slug=uuid.uuid4().hex)
    db_session.add(session)
    await db_session.flush()
    db_session.add_all([
        SessionPricing(session_id=session.id, pricing_type="MONTHLY", price=2500, currency="INR"),
        SessionInstructor(session_id=session.id, member_id=teacher.id, role="PRIMARY"),
        SessionEnrollment(organization_id=org.id, session_id=session.id, user_id=student.id,
                          valid_from=datetime(2026, 9, 15, tzinfo=timezone.utc), status="ACTIVE",
                          billing_value=1800, billing_currency="INR", billing_type="MONTHLY",
                          billing_label="Monthly Yoga"),
    ])
    # A paid order must not influence the subscription-value report.
    db_session.add(Order(organization_id=org.id, user_id=student.id, order_number=f"PR-{uuid.uuid4().hex}", total_amount=9999, status="PAID"))
    await db_session.commit()

    response = await client.get(
        f"organizations/{org.id}/billing/summary?start_date=2026-09-01&end_date=2026-09-30&interval=monthly&teacher_id={teacher.id}",
        headers=auth(owner),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_billed"] == 1800
    assert data["billing_count"] == 1
    assert data["rows"][0]["teacher_names"] == ["Asha Teacher"]
    assert data["rows"][0]["source"] == "SUBSCRIPTION_VALUE"

    csv_response = await client.get(
        f"organizations/{org.id}/billing/export.csv?start_date=2026-09-01&end_date=2026-09-30",
        headers=auth(owner),
    )
    assert csv_response.status_code == 200
    assert "Monthly Yoga" in csv_response.text
    assert "9999" not in csv_response.text
    assert "Sessions taken" in csv_response.text
    assert "Sessions excused/cancelled" in csv_response.text


@pytest.mark.asyncio
async def test_teacher_cannot_access_owner_billing(client, db_session):
    teacher = User(email=f"teacher-{uuid.uuid4()}@example.com", full_name="Teacher", role="TEACHER", is_verified=True, account_status="ACTIVE")
    db_session.add(teacher)
    await db_session.flush()
    org = Organization(name="Private Billing", slug=uuid.uuid4().hex)
    db_session.add(org)
    await db_session.flush()
    db_session.add(OrganizationMember(organization_id=org.id, user_id=teacher.id, role="TEACHER"))
    await db_session.commit()
    response = await client.get(
        f"organizations/{org.id}/billing/summary?start_date=2026-09-01&end_date=2026-09-30",
        headers=auth(teacher),
    )
    assert response.status_code == 403
