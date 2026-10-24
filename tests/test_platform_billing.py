"""Billing invariants: qualifying clicks, unique students, calendar months and tenant access."""
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock
import httpx
import pytest
from sqlalchemy import func, select
from app.core.database import get_db
from app.core.security import create_access_token
from app.main import app, api_v1
from app.models.commerce import Order, SessionEnrollment
from app.models.organization import Organization, OrganizationMember, OrganizationSetting, OrganizationStudent
from app.models.platform_billing import PlatformInvoice, StudentJoinEvent, StudentJoinTeacher
from app.models.schedule import ClassOccurrence
from app.models.session import ClassType, Session, SessionInstructor
from app.models.user import User
from app.modules.billing import platform
from app.modules.student.router import get_class_meeting_url


async def setup(db):
    org = Organization(name="Billing studio", slug=str(uuid.uuid4()), timezone="Asia/Kolkata")
    owner = User(email=f"{uuid.uuid4()}@test.com", full_name="Owner", role="STUDIO", billing_country="IN", is_verified=True, account_status="ACTIVE")
    teacher = User(email=f"{uuid.uuid4()}@test.com", full_name="Teacher", role="TEACHER", billing_country="IN", is_verified=True, account_status="ACTIVE")
    db.add_all([org, owner, teacher]); await db.flush()
    om = OrganizationMember(organization_id=org.id, user_id=owner.id, role="STUDIO", can_teach=False)
    tm = OrganizationMember(organization_id=org.id, user_id=teacher.id, role="TEACHER", can_teach=True)
    ct = ClassType(organization_id=org.id, name="Yoga", slug=str(uuid.uuid4()))
    db.add_all([om, tm, ct]); await db.flush()
    session = Session(organization_id=org.id, class_type_id=ct.id, name="Yoga class", slug=str(uuid.uuid4()))
    db.add(session); await db.flush()
    db.add(SessionInstructor(session_id=session.id, member_id=tm.id))
    now = datetime.now(timezone.utc)
    occ = ClassOccurrence(organization_id=org.id, session_id=session.id, instructor_id=tm.id,
        scheduled_start_at=now, scheduled_end_at=now+timedelta(hours=1), actual_start_at=now,
        actual_end_at=now+timedelta(hours=1), meeting_url="https://example.com/join")
    db.add(occ); await db.flush()
    return org, owner, om, tm, session, occ


async def student(db, org, session, **overrides):
    if not await db.scalar(select(OrganizationSetting).where(OrganizationSetting.organization_id == org.id)):
        db.add(OrganizationSetting(organization_id=org.id, country="India"))
    user = User(email=f"{uuid.uuid4()}@test.com", full_name="Student", role="STUDENT", billing_country="IN", is_verified=True, account_status="ACTIVE")
    db.add(user); await db.flush()
    now = datetime.now(timezone.utc)
    values = dict(organization_id=org.id, user_id=user.id, session_id=session.id, valid_from=now-timedelta(days=1),
        valid_until=now+timedelta(days=30), status="ACTIVE", billing_label="Monthly yoga", billing_value=500)
    values.update(overrides)
    enrollment = SessionEnrollment(**values)
    db.add_all([enrollment, OrganizationStudent(organization_id=org.id, user_id=user.id)])
    await db.flush()
    return user, enrollment


@pytest.mark.parametrize("count,amount", [(0, 0), (15, 0), (20, 0), (21, 630), (50, 1500)])
def test_pricing_boundary(count, amount):
    assert platform.fee(count) == amount


def test_studio_calendar_boundaries():
    org = Organization(timezone="Asia/Kolkata")
    assert platform.month_at(org, datetime(2026, 10, 31, 18, 29, tzinfo=timezone.utc)) == date(2026, 10, 1)
    assert platform.month_at(org, datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc)) == date(2026, 11, 1)
    assert platform.month_bounds(org, date(2026, 12, 1))[1] == datetime(2026, 12, 31, 18, 30, tzinfo=timezone.utc)
    assert platform.month_bounds(org, date(2028, 2, 1))[1] == datetime(2028, 2, 29, 18, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize("country", ["US", None])
async def test_non_domestic_usage_requires_quote_and_no_automatic_statement(db_session, country):
    org, owner, om, tm, session, occ = await setup(db_session)
    user, enrollment = await student(db_session, org, session)
    user.billing_country = country
    await platform.record_join(db_session, org, user, enrollment, occ)
    await db_session.flush()
    data = await platform.dashboard(db_session, org)
    assert data["active_students"] == 1 and data["billing_status"] == "CONTACT_SUPPORT"
    assert data["platform_amount"] is None
    event = await db_session.scalar(select(StudentJoinEvent).where(StudentJoinEvent.organization_id == org.id))
    event.month = date(2026, 1, 1)
    await db_session.flush()
    data = await platform.dashboard(db_session, org, date(2026, 1, 1))
    assert data["billing_status"] == "CONTACT_SUPPORT" and data["invoices"] == []


@pytest.mark.asyncio
async def test_join_records_repeats_and_snapshots_teacher(db_session):
    org, owner, om, tm, session, occ = await setup(db_session)
    user, enrollment = await student(db_session, org, session)
    await db_session.commit()
    await get_class_meeting_url(occ.id, user, db_session)
    await get_class_meeting_url(occ.id, user, db_session)
    data = await platform.dashboard(db_session, org)
    assert data["active_students"] == 1 and data["join_clicks"] == 2 and data["platform_amount"] == 0
    assert data["total_students"] == 1 and data["total_teachers"] == 1
    assert data["active_subscriptions"] == 1
    filtered = await platform.dashboard(db_session, org, teacher_id=tm.id)
    assert filtered["active_students"] == 1
    # Later expiry or attendance edits cannot erase a qualifying join.
    enrollment.status = "EXPIRED"
    occ.instructor_id = None
    await db_session.flush()
    filtered = await platform.dashboard(db_session, org, teacher_id=tm.id)
    assert filtered["active_students"] == 1 and filtered["active_subscriptions"] == 0
    other, _, _, _, other_session, other_occ = await setup(db_session)
    other_enrollment = SessionEnrollment(organization_id=other.id, user_id=user.id, session_id=other_session.id,
        valid_from=datetime.now(timezone.utc)-timedelta(days=1))
    db_session.add(other_enrollment); await db_session.flush()
    await platform.record_join(db_session, other, user, other_enrollment, other_occ)
    other_data = await platform.dashboard(db_session, other)
    assert other_data["active_students"] == 1 and other_data["join_clicks"] == 1
    assert (await platform.dashboard(db_session, org))["join_clicks"] == 2


@pytest.mark.asyncio
async def test_denied_or_failed_joins_do_not_count(db_session, monkeypatch):
    from fastapi import HTTPException
    org, owner, om, tm, session, occ = await setup(db_session)
    user, enrollment = await student(db_session, org, session, status="PAUSED")
    await db_session.commit()
    with pytest.raises(HTTPException):
        await get_class_meeting_url(occ.id, user, db_session)
    enrollment.status = "ACTIVE"; enrollment.valid_from = datetime.now(timezone.utc)+timedelta(days=1)
    await db_session.commit()
    with pytest.raises(HTTPException):
        await get_class_meeting_url(occ.id, user, db_session)
    enrollment.valid_from = datetime.now(timezone.utc)-timedelta(days=1)
    enrollment.valid_until = datetime.now(timezone.utc)-timedelta(minutes=1)
    await db_session.commit()
    with pytest.raises(HTTPException):
        await get_class_meeting_url(occ.id, user, db_session)
    enrollment.valid_until = datetime.now(timezone.utc)+timedelta(days=1)
    enrollment.total_sessions = 1; enrollment.sessions_attended = 1
    await db_session.commit()
    with pytest.raises(HTTPException):
        await get_class_meeting_url(occ.id, user, db_session)
    enrollment.sessions_attended = 0
    await db_session.commit()
    monkeypatch.setattr("app.modules.student.router.get_or_create_occurrence_meeting", AsyncMock(side_effect=HTTPException(503, "Provider unavailable")))
    with pytest.raises(HTTPException):
        await get_class_meeting_url(occ.id, user, db_session)
    assert await db_session.scalar(select(func.count(StudentJoinEvent.id)).where(StudentJoinEvent.organization_id == org.id)) == 0


@pytest.mark.asyncio
async def test_unique_studio_counts_teacher_overlap_and_frozen_invoices(db_session):
    org, owner, om, tm, session, occ = await setup(db_session)
    # The owner is already a studio member; make it teach rather than duplicating it.
    om.can_teach = True
    now = datetime.now(timezone.utc)
    past = date(2026, 1, 1)
    for index in range(21):
        user, enrollment = await student(db_session, org, session)
        for _ in range(2):
            event = StudentJoinEvent(pricing_region="INDIA", organization_id=org.id, user_id=user.id, enrollment_id=enrollment.id,
                occurrence_id=occ.id, month=past, clicked_at=datetime(2026, 1, 15, tzinfo=timezone.utc))
            db_session.add(event); await db_session.flush()
            db_session.add_all([StudentJoinTeacher(event_id=event.id, teacher_id=tm.id),
                StudentJoinTeacher(event_id=event.id, teacher_id=om.id)])
    await db_session.flush()
    first = await platform.dashboard(db_session, org, past, tm.id, now=now)
    assert first["active_students"] == 21 and first["studio_active_students"] == 21
    assert first["join_clicks"] == 42 and first["platform_amount"] == 630
    assert len(first["invoices"]) == 1
    invoice_id = first["invoices"][0]["id"]
    again = await platform.dashboard(db_session, org, past, om.id, now=now)
    assert again["invoices"][0]["id"] == invoice_id and again["active_students"] == 21
    assert await platform.finalize_months(db_session, org, now) == 0
    assert await db_session.scalar(select(func.count(PlatformInvoice.id)).where(PlatformInvoice.organization_id == org.id)) == 1
    assert (await platform.dashboard(db_session, org, now=now))["active_students"] == 0


@pytest.mark.asyncio
async def test_multi_class_pass_is_one_subscription_and_revenue_is_not_multiplied(db_session):
    org, owner, om, tm, session, occ = await setup(db_session)
    user, enrollment = await student(db_session, org, session)
    order = Order(organization_id=org.id, user_id=user.id, order_number=str(uuid.uuid4()), total_amount=500, currency="INR", status="PAID")
    pending = Order(organization_id=org.id, user_id=user.id, order_number=str(uuid.uuid4()), total_amount=999, status="PENDING")
    foreign_currency = Order(organization_id=org.id, user_id=user.id, order_number=str(uuid.uuid4()), total_amount=10, currency="USD", status="PAID")
    db_session.add_all([order, pending, foreign_currency]); await db_session.flush()
    enrollment.purchase_order_id = order.id
    second_class = Session(organization_id=org.id, class_type_id=session.class_type_id, name="Second class", slug=str(uuid.uuid4()))
    db_session.add(second_class); await db_session.flush()
    db_session.add_all([SessionInstructor(session_id=second_class.id, member_id=tm.id),
        SessionEnrollment(organization_id=org.id, user_id=user.id, session_id=second_class.id, purchase_order_id=order.id,
            billing_label="Monthly yoga", valid_from=enrollment.valid_from, valid_until=enrollment.valid_until)])
    await db_session.flush()
    data = await platform.dashboard(db_session, org)
    assert data["active_subscriptions"] == 1
    assert data["active_plans"] == [{"name": "Monthly yoga", "subscriptions": 1}]
    assert {row["currency"]: row["amount"] for row in data["total_revenue"]} == {"INR": 500, "USD": 10}
    filtered = await platform.dashboard(db_session, org, teacher_id=tm.id)
    assert filtered["total_revenue"] == [{"currency": "INR", "amount": 500}]


@pytest.mark.asyncio
async def test_dashboard_access_and_private_profile(db_session):
    org, owner, om, tm, session, occ = await setup(db_session)
    user, enrollment = await student(db_session, org, session)
    admin = await db_session.scalar(select(User).where(User.role == "SUPER_ADMIN"))
    if not admin:
        admin = User(email=f"{uuid.uuid4()}@test.com", full_name="Admin", role="SUPER_ADMIN", is_superadmin=True, is_verified=True, account_status="ACTIVE")
    profile = await db_session.scalar(select(OrganizationSetting).where(OrganizationSetting.organization_id == org.id))
    profile.bank_name, profile.account_number_enc = "Example Bank", "123456789"
    profile.address_line1, profile.zoom_host_key = "Studio street", "must-not-leak"
    db_session.add(admin)
    await db_session.commit()
    async def dependency():
        yield db_session
    api_v1.dependency_overrides[get_db] = dependency
    def headers(actor):
        return {"Authorization": f"Bearer {create_access_token({'sub': str(actor.id)})}"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1") as client:
            path = f"/organizations/{org.id}/billing/progressrooms"
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers=headers(user))).status_code == 403
            teacher_user = await db_session.get(User, tm.user_id)
            assert (await client.get(path, headers=headers(teacher_user))).status_code == 403
            assert (await client.get(path, headers=headers(owner))).status_code == 200
            assert (await client.get(f"/admin/organizations/{org.id}", headers=headers(owner))).status_code == 403
            assert (await client.get("/admin/organizations", headers=headers(owner))).status_code == 403
            directory = (await client.get("/admin/organizations", headers=headers(admin))).json()
            studio_row = next(row for row in directory if row["id"] == str(org.id))
            assert studio_row["owner"] == {"name": owner.full_name, "email": owner.email}
            assert studio_row["total_teachers"] == 1 and studio_row["total_students"] == 1
            assert studio_row["active_students"] == 0 and studio_row["estimated_platform_fee"] == 0
            assert "profile" not in studio_row and "account_number" not in studio_row
            profile = (await client.get(f"/admin/organizations/{org.id}", headers=headers(admin))).json()
            assert profile["profile"]["account_number"] == "123456789"
            assert profile["profile"]["address_line1"] == "Studio street"
            assert profile["website_url"] is None
            assert "must-not-leak" not in str(profile) and "teachers" not in profile and "students" not in profile
            other = Organization(name="Other", slug=str(uuid.uuid4()))
            db_session.add(other); await db_session.flush()
            assert (await client.get(f"/organizations/{other.id}/billing/progressrooms", headers=headers(owner))).status_code == 403
            assert (await client.get(f"/admin/organizations/{other.id}/progressrooms?teacher_id={tm.id}", headers=headers(admin))).status_code == 404
            assert (await client.get(path+"?month=2026-01-15", headers=headers(owner))).status_code == 422
    finally:
        api_v1.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_studio_website_link_requires_available_published_artifact(db_session, monkeypatch, tmp_path):
    from app.modules.admin.router import studio_details
    from app.models.website import OrganizationWebsite, WebsiteTemplate
    org, _, _, _, _, _ = await setup(db_session)
    template = WebsiteTemplate(name="Studio website", code=str(uuid.uuid4()))
    db_session.add(template); await db_session.flush()
    site = OrganizationWebsite(organization_id=org.id, template_id=template.id, is_published=True,
        custom_sections=[{"website_release": {"version": uuid.uuid4().hex}}])
    db_session.add(site); await db_session.flush()
    # Missing export storage does not break the admin profile.
    assert (await studio_details(org.id, None, db_session))["website_url"] is None
    artifact = tmp_path / "index.html"
    artifact.write_text("<h1>Studio website</h1>")
    monkeypatch.setattr("app.modules.websites.rendering.version_path", lambda *args: artifact)
    assert (await studio_details(org.id, None, db_session))["website_url"] == f"/{org.slug}"
    site.is_published = False; await db_session.flush()
    assert (await studio_details(org.id, None, db_session))["website_url"] is None
