import uuid
from datetime import datetime, timedelta, timezone
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from app.main import app, api_v1
from app.core.database import get_db
from app.core.security import create_access_token
from app.models.user import User
from app.models.organization import Organization, OrganizationMember, OrganizationStudent
from app.models.session import Session, ClassType
from app.models.commerce import SessionEnrollment
from app.models.schedule import ClassOccurrence

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

@pytest_asyncio.fixture
async def roster(db_session):
    org = Organization(name="Studio", slug=uuid.uuid4().hex)
    other = Organization(name="Other", slug=uuid.uuid4().hex)
    db_session.add_all([org, other]); await db_session.flush()
    people = []
    for role, studio in [("STUDIO", org), ("TEACHER", org), ("STUDENT", org), ("TEACHER", other)]:
        user = User(email=f"{uuid.uuid4().hex}@example.com", full_name=role, role=role, is_verified=True)
        db_session.add(user); await db_session.flush()
        if role != "STUDENT":
            db_session.add(OrganizationMember(organization_id=studio.id, user_id=user.id, role=role))
        people.append(user)
    owner, teacher, student, outsider = people
    record = OrganizationStudent(organization_id=org.id, user_id=student.id)
    ct = ClassType(organization_id=org.id, name="Yoga", slug=uuid.uuid4().hex)
    db_session.add_all([record, ct]); await db_session.flush()
    cls = Session(organization_id=org.id, class_type_id=ct.id, name="Monthly yoga", slug=uuid.uuid4().hex)
    db_session.add(cls); await db_session.flush()
    now = datetime.now(timezone.utc)
    enrollment = SessionEnrollment(organization_id=org.id, session_id=cls.id, user_id=student.id,
        valid_from=now-timedelta(days=20), valid_until=now+timedelta(days=10), total_sessions=12, sessions_attended=4)
    occ = ClassOccurrence(organization_id=org.id, session_id=cls.id,
        actual_start_at=now, actual_end_at=now+timedelta(hours=1),
        scheduled_start_at=now, scheduled_end_at=now+timedelta(hours=1))
    db_session.add_all([enrollment, occ]); await db_session.commit()
    return owner, teacher, student, outsider, record, enrollment, occ, cls

@pytest.mark.asyncio
async def test_profile_validation_and_tenant_boundaries(client, db_session, roster):
    owner, teacher, student, outsider, record, enrollment, _, _ = roster
    url=f"students/{record.id}"
    body=dict(full_name="Updated Student", phone="+91 98765 43210", whatsapp_number="+91 91234 56789", address="Studio road", location="Pune", student_notes="Practice note")
    for user in [outsider, student]:
        assert (await client.patch(url, headers=auth(user), json=body)).status_code == 403
    for bad in [{"full_name":"Name"}, {**body,"phone":None}, {**body,"phone":" "}, {**body,"phone":"123"}, {**body,"whatsapp_number":"bad"}, {**body,"full_name":" "}, {**body,"status":"UNKNOWN"}, {**body,"is_verified":False}, {**body,"email":"other@example.com"}]:
        assert (await client.patch(url, headers=auth(owner), json=bad)).status_code == 422
    assert (await client.patch(url, headers=auth(teacher), json=body)).status_code == 200
    await db_session.refresh(student)
    assert student.full_name == "Updated Student" and student.whatsapp_number == body["whatsapp_number"]
    assert student.is_verified and enrollment.sessions_attended == 4
    result=(await client.get("students", headers=auth(owner), params={"search":"98765"})).json()["items"]
    assert result[0]["phone"] == body["phone"] and result[0]["address"] == body["address"]
    assert (await client.patch("auth/me", headers=auth(student), json={"full_name":"X", "phone":None})).status_code == 422

@pytest.mark.asyncio
async def test_pause_resume_preserves_time_and_blocks_access(client, db_session, roster):
    owner, teacher, student, outsider, record, enrollment, occ, _ = roster
    base=f"students/{record.id}/enrollments/{enrollment.id}"
    for user in [student, outsider]:
        assert (await client.post(base+"/pause", headers=auth(user))).status_code == 403
    assert (await client.post(base+"/pause", headers=auth(teacher))).status_code == 200
    assert enrollment.status == "PAUSED" and enrollment.paused_at
    assert (await client.post(base+"/pause", headers=auth(teacher))).status_code == 409
    assert (await client.get(f"student/classes/{occ.id}/meeting-url", headers=auth(student))).status_code == 403
    assert (await client.post(f"organizations/{record.organization_id}/occurrences/{occ.id}/attendance", headers=auth(owner), json={"enrollment_id":str(enrollment.id),"status":"ATTENDED"})).status_code == 404
    # Simulate 20 days away: original expiry has passed, 10 days were frozen.
    enrollment.paused_at = datetime.now(timezone.utc)-timedelta(days=20)
    enrollment.valid_until = enrollment.paused_at+timedelta(days=10)
    before=enrollment.valid_until
    await db_session.commit()
    assert (await client.post(base+"/resume", headers=auth(owner))).status_code == 200
    assert abs((enrollment.valid_until-datetime.now(timezone.utc)).total_seconds()-10*86400) < 2
    assert enrollment.status == "ACTIVE" and enrollment.paused_at is None and enrollment.sessions_attended == 4
    assert (await client.post(base+"/resume", headers=auth(owner))).status_code == 409
    assert enrollment.valid_until > before
    enrollment.valid_until=datetime.now(timezone.utc)-timedelta(seconds=1)
    await db_session.commit()
    assert (await client.post(base+"/pause", headers=auth(owner))).status_code == 409

@pytest.mark.asyncio
async def test_inactive_classes_can_be_selected_for_memberships(client, db_session, roster):
    owner, _, _, _, record, _, _, cls = roster
    cls.is_active=False
    await db_session.commit()
    url=f"organizations/{record.organization_id}"
    classes=(await client.get(url+"/classes", headers=auth(owner))).json()
    assert any(c["id"] == str(cls.id) and c["is_active"] is False for c in classes)
    response=await client.post(url+"/membership-tiers", headers=auth(owner), json={"name":"Flexible", "tier_type":"MONTHLY", "price":100, "validity_days":30, "class_ids":[str(cls.id)]})
    assert response.status_code == 201, response.text
    assert response.json()["attached_classes"][0]["is_active"] is False

@pytest.mark.asyncio
async def test_cancel_pass_marks_student_inactive(client, db_session, roster):
    owner, _, _, _, record, enrollment, _, _ = roster
    response = await client.post(f"students/{record.id}/enrollments/{enrollment.id}/cancel", headers=auth(owner))
    assert response.status_code == 200, response.text
    assert enrollment.status == "CANCELLED" and enrollment.paused_at is None
    await db_session.refresh(record)
    assert record.status == "INACTIVE"
    assert (await client.post(f"students/{record.id}/enrollments/{enrollment.id}/cancel", headers=auth(owner))).status_code == 409

@pytest.mark.asyncio
async def test_deactivating_teacher_deactivates_assigned_classes(client, db_session, roster):
    owner, teacher, _, _, record, _, _, cls = roster
    from app.models.session import SessionInstructor
    teacher_member = await db_session.scalar(select(OrganizationMember).where(OrganizationMember.user_id == teacher.id))
    db_session.add(SessionInstructor(session_id=cls.id, member_id=teacher_member.id, role="PRIMARY"))
    await db_session.commit()
    response = await client.put(f"teachers/{teacher_member.id}", headers=auth(owner), json={"is_active": False})
    assert response.status_code == 200, response.text
    await db_session.refresh(teacher_member); await db_session.refresh(cls)
    assert not teacher_member.is_active and not cls.is_active
    assert response.json()["deactivated_class_count"] == 1

@pytest.mark.asyncio
async def test_registration_requires_phone_and_stores_whatsapp(client, db_session, monkeypatch):
    async def no_email(**kwargs): pass
    monkeypatch.setattr("app.modules.auth.service.send_otp_email", no_email)
    body=dict(full_name="Student", email=f"{uuid.uuid4().hex}@example.com", password="long-password", location="Pune", role="STUDENT", privacy_accepted=True, privacy_policy_version="2026-09-27")
    for phone in [None, "", "123", "not a phone"]:
        assert (await client.post("auth/register", json={**body,"phone":phone})).status_code == 422
    response=await client.post("auth/register", json={**body,"phone":"+91 98765 43210", "whatsapp_number":"+91 91234 56789"})
    assert response.status_code == 200, response.text
    from sqlalchemy import select
    student=await db_session.scalar(select(User).where(User.email == body["email"]))
    assert student.whatsapp_number == "+91 91234 56789"
