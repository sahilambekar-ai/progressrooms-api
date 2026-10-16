import pytest
import pytest_asyncio
import uuid
import httpx
from app.main import app, api_v1
from app.core.database import get_db
from app.models.user import User
from app.models.organization import Organization, OrganizationMember
from app.models.session import ClassType, Session, SessionPricing
from app.core.security import create_access_token

@pytest_asyncio.fixture
async def client(db_session):
    async def dependency():
        yield db_session
    api_v1.dependency_overrides[get_db] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1/") as client:
        yield client
    api_v1.dependency_overrides.clear()

def auth(user):
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}

@pytest.mark.asyncio
async def test_class_update_and_toggle(client, db_session):
    user = User(
        email=f"owner_{uuid.uuid4().hex[:6]}@example.com",
        full_name="Owner Test",
        role="STUDIO",
        is_verified=True,
        account_status="ACTIVE"
    )
    db_session.add(user)
    await db_session.flush()

    org = Organization(
        name="Yoga Studio",
        slug=f"yoga-{uuid.uuid4().hex[:6]}",
        timezone="Asia/Kolkata"
    )
    db_session.add(org)
    await db_session.flush()

    member = OrganizationMember(
        organization_id=org.id,
        user_id=user.id,
        role="STUDIO",
        is_active=True
    )
    db_session.add(member)
    await db_session.flush()

    class_type = ClassType(
        organization_id=org.id,
        name="Vinyasa",
        slug=f"vinyasa-{uuid.uuid4().hex[:6]}"
    )
    db_session.add(class_type)
    await db_session.flush()

    session = Session(
        organization_id=org.id,
        class_type_id=class_type.id,
        name="Sunrise Vinyasa",
        slug=f"sunrise-{uuid.uuid4().hex[:6]}",
        capacity=15,
        duration_minutes=60,
        is_active=True,
        is_public=True
    )
    db_session.add(session)
    await db_session.flush()

    pricing = SessionPricing(
        session_id=session.id,
        pricing_type="MONTHLY",
        price=1500.0,
        currency="INR",
        validity_days=30
    )
    db_session.add(pricing)
    await db_session.commit()

    headers = auth(user)

    # Test toggle status
    resp = await client.patch(
        f"organizations/{org.id}/classes/{session.id}/toggle-status",
        json={"is_active": False},
        headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # Test toggle again without payload
    resp = await client.patch(
        f"organizations/{org.id}/classes/{session.id}/toggle-status",
        headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    # Test update class details
    resp = await client.put(
        f"organizations/{org.id}/classes/{session.id}",
        json={
            "name": "Updated Sunrise Flow",
            "capacity": 25,
            "duration_minutes": 75,
            "is_active": True,
            "pricing": {
                "pricing_type": "MONTHLY",
                "price": 2000.0,
                "currency": "INR",
                "validity_days": 30
            }
        },
        headers=headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Sunrise Flow"
    assert data["is_active"] is True

    # Deletion is scoped to owners and keeps history while stopping future classes.
    from datetime import datetime, timedelta, timezone, time
    from sqlalchemy import select
    from app.models.schedule import ClassOccurrence, SessionScheduleRule
    now = datetime.now(timezone.utc)
    rule = SessionScheduleRule(session_id=session.id, day_of_week=0, start_time=time(9),
                               end_time=time(10), effective_start_date=now.date())
    occurrences = []
    for days, occurrence_status in [(-2, "COMPLETED"), (2, "SCHEDULED")]:
        start = now + timedelta(days=days)
        occurrence = ClassOccurrence(session_id=session.id, organization_id=org.id,
            scheduled_start_at=start, scheduled_end_at=start + timedelta(hours=1),
            actual_start_at=start, actual_end_at=start + timedelta(hours=1), status=occurrence_status)
        db_session.add(occurrence)
        occurrences.append(occurrence)
    outsider = User(email=f"outsider-{uuid.uuid4()}@example.com", full_name="Other owner",
                    role="STUDIO", is_verified=True, account_status="ACTIVE")
    db_session.add_all([rule, outsider])
    await db_session.commit()
    path = f"organizations/{org.id}/classes/{session.id}"
    assert (await client.delete(path, headers=auth(outsider))).status_code == 403
    assert (await client.delete(path)).status_code == 401
    await client.patch(path + "/toggle-status", json={"is_active": False}, headers=headers)
    assert (await client.get(f"organizations/{org.id}/classes?history=true", headers=headers)).json() == []
    assert len((await client.get(f"organizations/{org.id}/classes", headers=headers)).json()) == 1
    response = await client.delete(path, headers=headers)
    assert response.status_code == 204, response.text
    assert response.content == b""
    await db_session.refresh(session)
    await db_session.refresh(rule)
    for occurrence in occurrences:
        await db_session.refresh(occurrence)
    assert session.deleted_at is not None and not session.is_active
    assert not rule.is_active
    assert occurrences[0].status == "COMPLETED"
    assert occurrences[1].status == "CANCELLED"
    assert await db_session.scalar(select(SessionPricing).where(SessionPricing.session_id == session.id)) is not None
    assert (await client.get(f"organizations/{org.id}/classes", headers=headers)).json() == []
    history = await client.get(f"organizations/{org.id}/classes?history=true", headers=headers)
    assert history.status_code == 200
    assert [item["id"] for item in history.json()] == [str(session.id)]
    assert history.json()[0]["deleted_at"]
    assert len(history.json()[0]["schedule_rules"]) == 1
    assert (await client.get(f"organizations/{org.id}/classes?history=true", headers=auth(outsider))).status_code == 403
    assert (await client.delete(path, headers=headers)).status_code == 404

@pytest.mark.asyncio
async def test_membership_tiers_with_attached_classes(client, db_session):
    user = User(
        email=f"owner_{uuid.uuid4().hex[:6]}@example.com",
        full_name="Owner Membership Test",
        role="STUDIO",
        is_verified=True,
        account_status="ACTIVE"
    )
    db_session.add(user)
    await db_session.flush()

    org = Organization(
        name="Pilates Studio",
        slug=f"pilates-{uuid.uuid4().hex[:6]}",
        timezone="Asia/Kolkata"
    )
    db_session.add(org)
    await db_session.flush()

    member = OrganizationMember(
        organization_id=org.id,
        user_id=user.id,
        role="STUDIO",
        is_active=True
    )
    db_session.add(member)
    await db_session.flush()

    class_type = ClassType(
        organization_id=org.id,
        name="Mat Pilates",
        slug=f"mat-{uuid.uuid4().hex[:6]}"
    )
    db_session.add(class_type)
    await db_session.flush()

    # Create 3 classes
    classes = []
    for i in range(3):
        s = Session(
            organization_id=org.id,
            class_type_id=class_type.id,
            name=f"Pilates Class {i+1}",
            slug=f"pilates-{i+1}-{uuid.uuid4().hex[:6]}",
            capacity=10,
            duration_minutes=50,
            is_active=True
        )
        db_session.add(s)
        classes.append(s)
    await db_session.commit()

    headers = auth(user)

    # Create membership tier with 2 attached classes
    resp = await client.post(
        f"organizations/{org.id}/membership-tiers",
        json={
            "name": "Mat Unlimited Pass",
            "tier_type": "MONTHLY",
            "price": 3500.0,
            "offer_price": 3000.0,
            "currency": "INR",
            "validity_days": 30,
            "description": "Unlimited access to Mat classes",
            "is_active": True,
            "class_ids": [str(classes[0].id), str(classes[1].id)]
        },
        headers=headers
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["name"] == "Mat Unlimited Pass"
    assert created["offer_price"] == 3000.0
    assert len(created["attached_classes"]) == 2
    assert len(created["attached_class_ids"]) == 2
    assert created["is_active"] is True

    tier_id = created["id"]

    # List membership tiers
    resp = await client.get(f"organizations/{org.id}/membership-tiers", headers=headers)
    assert resp.status_code == 200
    tiers = resp.json()
    assert len(tiers) == 1
    assert tiers[0]["id"] == tier_id
    assert len(tiers[0]["attached_classes"]) == 2

    # Toggle status
    resp = await client.patch(
        f"organizations/{org.id}/membership-tiers/{tier_id}/toggle-status",
        json={"is_active": False},
        headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # Update membership tier (attach 3rd class, change price, reactivate)
    resp = await client.put(
        f"organizations/{org.id}/membership-tiers/{tier_id}",
        json={
            "price": 4000.0,
            "is_active": True,
            "class_ids": [str(classes[0].id), str(classes[1].id), str(classes[2].id)]
        },
        headers=headers
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["price"] == 4000.0
    assert updated["is_active"] is True
    assert len(updated["attached_classes"]) == 3

    # Validate edits against the stored counterpart, not just supplied fields.
    for payload in [{"price": 2000}, {"offer_price": 5000}, {"offer_price": -1},
                    {"offer_price": 1.001}, {"offer_price": "NaN"}]:
        response = await client.put(f"organizations/{org.id}/membership-tiers/{tier_id}",
                                    json=payload, headers=headers)
        assert response.status_code == 422, response.text
    response = await client.put(f"organizations/{org.id}/membership-tiers/{tier_id}",
                                json={"offer_price": None, "price": 2000}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["offer_price"] is None
    # Equal and zero offers are valid and round-trip; omission preserves the offer.
    for offer in [2000, 0]:
        response = await client.put(f"organizations/{org.id}/membership-tiers/{tier_id}",
                                    json={"offer_price": offer}, headers=headers)
        assert response.status_code == 200, response.text
        response = await client.put(f"organizations/{org.id}/membership-tiers/{tier_id}",
                                    json={"name": "Updated pass"}, headers=headers)
        assert response.json()["offer_price"] == offer
    for offer in [-1, 3501, 1.001]:
        response = await client.post(f"organizations/{org.id}/membership-tiers",
                                    json={"name": "Invalid offer", "price": 3500, "offer_price": offer},
                                    headers=headers)
        assert response.status_code == 422, response.text

    await client.put(f"organizations/{org.id}/membership-tiers/{tier_id}", json={"is_active": False}, headers=headers)
    assert (await client.get(f"organizations/{org.id}/membership-tiers?history=true", headers=headers)).json() == []
    assert len((await client.get(f"organizations/{org.id}/membership-tiers", headers=headers)).json()) == 1
    response = await client.delete(f"organizations/{org.id}/membership-tiers/{tier_id}", headers=headers)
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert (await client.get(f"organizations/{org.id}/membership-tiers", headers=headers)).json() == []
    assert (await client.delete(f"organizations/{org.id}/membership-tiers/{tier_id}", headers=headers)).status_code == 404

    history = await client.get(f"organizations/{org.id}/membership-tiers?history=true", headers=headers)
    assert history.status_code == 200
    assert [item["id"] for item in history.json()] == [tier_id]
    assert history.json()[0]["deleted_at"]
    assert len(history.json()[0]["attached_classes"]) == 3
    assert (await client.get(f"organizations/{org.id}/membership-tiers?history=true")).status_code == 401
