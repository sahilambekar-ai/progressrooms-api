import uuid
from datetime import datetime, timedelta, timezone
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.main import app, api_v1
from app.core.database import get_db
from app.core.security import create_access_token, get_password_hash
from app.models.user import User
from app.models.organization import Organization, OrganizationMember, OrganizationStudent
from app.models.session import ClassType, Session
from app.models.schedule import ClassOccurrence
from app.models.commerce import SessionEnrollment
from app.modules.auth.service import AuthService

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

async def staff(db, role, org=None):
    user = User(email=f"{uuid.uuid4().hex}@example.com", full_name=role, role=role, phone="+91 98765 43210",
                password_hash=get_password_hash("strong-password"), is_verified=True,
                is_superadmin=role == "SUPER_ADMIN")
    db.add(user)
    await db.flush()
    member = None
    if role in {"STUDIO", "TEACHER"}:
        if not org:
            org = Organization(name="Studio", slug=uuid.uuid4().hex)
            db.add(org)
            await db.flush()
        member = OrganizationMember(organization_id=org.id, user_id=user.id, role=role)
        db.add(member)
        await db.flush()
    await db.commit()
    return user, org, member

@pytest.mark.asyncio
async def test_teacher_credentials_and_owner_boundaries(client, db_session):
    owner, org, _ = await staff(db_session, "STUDIO")
    other_owner, _, _ = await staff(db_session, "STUDIO")
    body = dict(name="Teacher One", email=f"{uuid.uuid4().hex}@example.com", username=f"teacher-{uuid.uuid4().hex}",
                password="secret-pass-123", phone="123456789", address="Studio Road", location="Pune", studio_id=str(org.id))
    assert (await client.post("teachers", json=body)).status_code == 401
    assert (await client.post("teachers", json=body, headers=auth(other_owner))).status_code == 403
    response = await client.post("teachers", json=body, headers=auth(owner))
    assert response.status_code == 201, response.text
    teacher = response.json()
    assert teacher["role"] == "TEACHER" and "password" not in teacher
    login = await client.post("auth/login", json={"email": body["email"], "password": body["password"]})
    assert login.status_code == 200, login.text
    assert login.json()["user"]["role"] == "TEACHER"
    assert login.json()["user"]["organization_id"] == str(org.id)
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.post("teachers", json=body, headers=headers)).status_code == 403
    for path in [f"organizations/{org.id}/profile", f"organizations/{org.id}/website"]:
        assert (await client.get(path, headers=headers)).status_code == 403
    assert (await client.post(f"organizations/{org.id}/class-types", json={"name": "X", "slug": "x"}, headers=headers)).status_code == 403
    assert (await client.put(f"teachers/{teacher['id']}", json={"name": "Hijack"}, headers=auth(other_owner))).status_code == 403
    assert (await client.post("teachers", json=body, headers=auth(owner))).status_code == 409
    assert (await client.get("auth/me", headers=headers)).json()["email"] == body["email"]
    assert (await client.delete(f"teachers/{teacher['id']}", headers=auth(owner))).status_code == 200
    assert (await client.get("auth/me", headers=headers)).status_code == 403

@pytest.mark.asyncio
async def test_student_registration_and_no_privilege_escalation(client, db_session, monkeypatch):
    async def no_email(**kwargs): pass
    monkeypatch.setattr("app.modules.auth.service.send_otp_email", no_email)
    monkeypatch.setattr("app.modules.auth.router.settings.AUTH_SHOW_OTP_PREVIEW", False)
    for role in ["TEACHER", "SUPER_ADMIN", "OWNER", "INSTRUCTOR"]:
        response = await client.post("auth/register", json=dict(full_name="X", email="x@example.com", password="long-password", role=role))
        assert response.status_code == 422
    email = f"{uuid.uuid4().hex}@example.com"
    response = await client.post("auth/register", json=dict(full_name="Student", email=email, password="long-password", phone="+91 98765 43210", location="Mumbai", role="STUDENT", privacy_accepted=True, privacy_policy_version="2026-09-27"))
    assert response.status_code == 200, response.text
    assert response.json()["otp_preview"] is None
    user = await db_session.scalar(select(User).where(User.email == email))
    assert user.role == "STUDENT" and user.location == "Mumbai"
    assert not await db_session.scalar(select(OrganizationStudent.id).where(OrganizationStudent.user_id == user.id))
    assert (await client.post("auth/request-otp", json={"email": "missing@example.com"})).status_code == 404
    assert (await client.get("auth/me", headers={"Authorization": "Bearer dev_test_token_superadmin"})).status_code == 401

@pytest.mark.asyncio
async def test_assigned_students_classes_join_links_and_attendance(client, db_session):
    owner, org, _ = await staff(db_session, "STUDIO")
    teacher, _, member = await staff(db_session, "TEACHER", org)
    other_teacher, other_org, other_member = await staff(db_session, "TEACHER")
    student, _, _ = await staff(db_session, "STUDENT")
    record = OrganizationStudent(organization_id=org.id, user_id=student.id)
    ct = ClassType(organization_id=org.id, name="Yoga", slug=uuid.uuid4().hex)
    db_session.add_all([record, ct]); await db_session.flush()
    cls = Session(organization_id=org.id, class_type_id=ct.id, name="Today's class", slug=uuid.uuid4().hex)
    db_session.add(cls); await db_session.flush()
    now = datetime.now(timezone.utc)
    occ = ClassOccurrence(organization_id=org.id, session_id=cls.id, instructor_id=member.id,
                          actual_start_at=now-timedelta(minutes=5), actual_end_at=now+timedelta(minutes=55),
                          scheduled_start_at=now-timedelta(minutes=5), scheduled_end_at=now+timedelta(minutes=55),
                          meeting_url="https://zoom.us/j/123456789", host_url="https://zoom.us/s/123456789")
    enr = SessionEnrollment(organization_id=org.id, session_id=cls.id, user_id=student.id, valid_from=now,
                            total_sessions=10, sessions_attended=0, status="ACTIVE")
    db_session.add_all([occ, enr]); await db_session.commit()
    h = auth(teacher); owner_h = auth(owner)
    # Each teacher can view all students under their studio
    assert (await client.get("students", headers=h)).json()["items"][0]["id"] == str(student.id)
    # Teachers from other studios cannot see this studio's students
    assert (await client.get("students", headers=auth(other_teacher))).json()["items"] == []
    # Teachers can update student notes directly
    assert (await client.patch(f"students/{record.id}", headers=h, json={"student_notes": "Needs practice", "phone": "+91 98765 43210"})).status_code == 200
    path = f"organizations/{org.id}/occurrences"
    agenda = (await client.get(f"{path}/teacher-agenda", headers=h)).json()
    assert agenda["day"] == "today" and agenda["classes"][0]["is_current"]
    link = await client.post(f"{path}/{occ.id}/join-link", headers=h)
    assert link.status_code == 200, link.text
    assert (await client.post(f"{path}/{occ.id}/join-link", headers=h)).json() == link.json()
    assert (await client.post(f"{path}/{occ.id}/join-link", headers=auth(other_teacher))).status_code == 403
    assert (await client.post(f"{path}/{occ.id}/cancel", headers=h, json={"reason": "x"})).status_code == 403
    attendance = f"{path}/{occ.id}/attendance"
    for _ in range(2):
        response = await client.post(attendance, headers=h, json={"enrollment_id": str(enr.id), "status": "ATTENDED"})
        assert response.status_code == 200, response.text
    await db_session.refresh(enr)
    assert enr.sessions_attended == 1
    assert (await client.get(attendance, headers=h)).json()[0]["attendance_status"] == "ATTENDED"
    unassigned, _, unassigned_member = await staff(db_session, "TEACHER", org)
    assert (await client.get(attendance, headers=auth(unassigned))).status_code == 404
    # When today has no classes, tomorrow's class is returned.
    occ.actual_start_at += timedelta(days=1); occ.actual_end_at += timedelta(days=1)
    await db_session.commit()
    agenda = (await client.get(f"{path}/teacher-agenda", headers=h)).json()
    assert agenda["day"] == "tomorrow" and not agenda["classes"][0]["is_current"]

@pytest.mark.asyncio
async def test_database_single_admin_and_valid_roles(db_session):
    first, _, _ = await staff(db_session, "SUPER_ADMIN")
    for role, flag in [("SUPER_ADMIN", True), ("INVALID", False), ("STUDENT", True)]:
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                db_session.add(User(email=f"{uuid.uuid4().hex}@example.com", full_name="Invalid", role=role, is_superadmin=flag))
                await db_session.flush()

@pytest.mark.asyncio
async def test_dev_login_uses_seeded_role_and_real_token(client, db_session, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "DEV_TEST_LOGIN_ENABLED", True)
    studio, org, _ = await staff(db_session, "STUDIO")
    studio.email = "owner@yogastudio.test"
    await db_session.commit()

    response = await client.post("auth/dev-login", json={"role": "STUDIO"})
    assert response.status_code == 200, response.text
    assert response.json()["user"]["role"] == "STUDIO"
    assert response.json()["user"]["organization_id"] == str(org.id)
    assert (await client.get("auth/me", headers={"Authorization": f"Bearer {response.json()['access_token']}"})).status_code == 200

@pytest.mark.asyncio
async def test_studio_creates_database_classes_teacher_cannot(client, db_session):
    from zoneinfo import ZoneInfo
    owner, org, _ = await staff(db_session, "STUDIO")
    teacher, _, member = await staff(db_session, "TEACHER", org)
    outsider, _, outside_member = await staff(db_session, "TEACHER")
    ct = ClassType(organization_id=org.id, name="Dance", slug=uuid.uuid4().hex)
    db_session.add(ct); await db_session.commit()
    today = datetime.now(ZoneInfo(org.timezone)).date()
    body = dict(class_type_id=str(ct.id), name="New class", slug=uuid.uuid4().hex,
                instructor_member_ids=[str(member.id)], pricing={"price": 100},
                schedule_rules=[dict(day_of_week=today.weekday(), start_time="18:00", end_time="19:00", effective_start_date=today.isoformat())])
    path = f"organizations/{org.id}/classes"
    assert (await client.post(path, headers=auth(teacher), json=body)).status_code == 403
    body["instructor_member_ids"] = [str(outside_member.id)]
    assert (await client.post(path, headers=auth(owner), json=body)).status_code == 422
    body["instructor_member_ids"] = [str(member.id)]
    response = await client.post(path, headers=auth(owner), json=body)
    assert response.status_code == 201, response.text
    class_id = uuid.UUID(response.json()["id"])
    assert await db_session.scalar(select(ClassOccurrence.id).where(ClassOccurrence.session_id == class_id))
    listed = (await client.get(path, headers=auth(teacher))).json()
    assert [c["id"] for c in listed] == [str(class_id)]

@pytest.mark.asyncio
async def test_wrong_password_and_otp_replay_fail(db_session, monkeypatch):
    async def no_email(**kwargs): pass
    monkeypatch.setattr("app.modules.auth.service.send_otp_email", no_email)
    from fastapi import HTTPException
    user, _, _ = await staff(db_session, "STUDENT")
    with pytest.raises(HTTPException):
        await AuthService.login_with_password(db_session, user.email, "wrong-password")
    otp, *_ = await AuthService.request_otp(db_session, user.email)
    await AuthService.verify_otp(db_session, user.email, otp)
    with pytest.raises(HTTPException):
        await AuthService.verify_otp(db_session, user.email, otp)

@pytest.mark.asyncio
async def test_owner_admits_registered_student_to_assigned_class(client, db_session):
    from app.models.session import SessionInstructor
    owner, org, _ = await staff(db_session, "STUDIO")
    teacher, _, member = await staff(db_session, "TEACHER", org)
    student, _, _ = await staff(db_session, "STUDENT")
    ct = ClassType(organization_id=org.id, name="General", slug=uuid.uuid4().hex)
    db_session.add(ct); await db_session.flush()
    cls = Session(organization_id=org.id, class_type_id=ct.id, name="Admitted class", slug=uuid.uuid4().hex)
    db_session.add(cls); await db_session.flush()
    db_session.add(SessionInstructor(session_id=cls.id, member_id=member.id, role="PRIMARY"))
    await db_session.commit()
    body = dict(email=student.email, teacher_member_id=str(member.id), class_id=str(cls.id))
    assert (await client.post("students", json=body, headers=auth(teacher))).status_code == 403
    response = await client.post("students", json=body, headers=auth(owner))
    assert response.status_code == 201, response.text
    assert (await client.post("students", json=body, headers=auth(owner))).status_code == 201
    enrollments = (await db_session.execute(select(SessionEnrollment).where(
        SessionEnrollment.user_id == student.id, SessionEnrollment.session_id == cls.id))).scalars().all()
    assert len(enrollments) == 1
    assert (await client.get("students", headers=auth(teacher))).json()["items"][0]["id"] == str(student.id)


@pytest.mark.asyncio
async def test_registration_privacy_consent_is_required_and_recorded(client, db_session, monkeypatch):
    from app.models.audit import AuditLog
    async def no_email(**kwargs): pass
    monkeypatch.setattr("app.modules.auth.service.send_otp_email", no_email)
    for role in ["STUDIO", "STUDENT"]:
        email = f"{uuid.uuid4().hex}@example.com"
        body = dict(full_name="Privacy Test", email=email, password="long-password",
                    phone="+91 98765 43210", location="Pune", role=role)
        for extra in [{}, {"privacy_accepted": False, "privacy_policy_version": "2026-09-27"},
                      {"privacy_accepted": "true", "privacy_policy_version": "2026-09-27"},
                      {"privacy_accepted": True, "privacy_policy_version": "old-version"}]:
            response = await client.post("auth/register", json={**body, **extra})
            assert response.status_code == 422, response.text
            assert not await db_session.scalar(select(User.id).where(User.email == email))
        response = await client.post("auth/register", json={**body, "privacy_accepted": True,
                                      "privacy_policy_version": "2026-09-27"})
        assert response.status_code == 200, response.text
        user = await db_session.scalar(select(User).where(User.email == email))
        consent = await db_session.scalar(select(AuditLog).where(AuditLog.user_id == user.id,
                                        AuditLog.action == "PRIVACY_POLICY_ACCEPTED"))
        assert consent.created_at is not None
        assert consent.new_data == {"policy_version": "2026-09-27", "audience": role,
                                    "accepted": True, "source": "registration"}
