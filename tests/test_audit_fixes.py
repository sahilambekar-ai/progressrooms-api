import pytest
import pytest_asyncio
import uuid
import httpx
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from httpx import AsyncClient

from app.main import app, api_v1
from app.core.database import get_db
from app.models.user import User
from app.models.organization import Organization, OrganizationMember, OrganizationStudent, OrganizationSetting
from app.models.session import ClassType, Session, SessionInstructor, SessionPricing
from app.models.schedule import SessionScheduleRule, ClassOccurrence
from app.models.commerce import SessionEnrollment
from app.models.support import SupportTicket, SupportMessage
from app.models.integration import OrganizationIntegration
from app.modules.scheduling.engine import SchedulingEngine
from app.modules.classes.router import update_session
from app.modules.classes.schemas import SessionUpdate
from app.modules.auth.service import AuthService
from app.common.enums import UserRole, SessionType, OccurrenceStatus, EnrollmentStatus
from app.core.security import create_access_token

@pytest_asyncio.fixture
async def client(db_session):
    async def dependency():
        yield db_session
    api_v1.dependency_overrides[get_db] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1/") as client:
        yield client
    api_v1.dependency_overrides.clear()

@pytest.fixture
def auth_headers():
    def _make(user: User):
        token = create_access_token({"sub": str(user.id), "role": user.role, "email": user.email})
        return {"Authorization": f"Bearer {token}"}
    return _make

@pytest.mark.asyncio
async def test_timezone_scheduling_and_midnight_rollover(db_session):
    """
    AUD-P0-3: Rule times in studio timezone (Asia/Kolkata) must be converted accurately to UTC.
    Midnight crossing (e.g. 23:30 to 00:30) must advance the end date by 1 day.
    """
    org = Organization(name="Timezone Shala", slug=f"tz-{uuid.uuid4().hex[:6]}", timezone="Asia/Kolkata")
    db_session.add(org)
    await db_session.flush()

    ct = ClassType(organization_id=org.id, name="Midnight Flow", slug=f"mf-{uuid.uuid4().hex[:6]}")
    db_session.add(ct)
    await db_session.flush()

    session = Session(
        organization_id=org.id,
        class_type_id=ct.id,
        name="Night Owl Yoga",
        slug=f"night-owl-{uuid.uuid4().hex[:6]}",
        session_type=SessionType.GROUP.value,
        duration_minutes=60,
        is_active=True
    )
    db_session.add(session)
    await db_session.flush()

    today_kolkata = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date()
    target_day = today_kolkata.weekday()

    # Rule: 23:30 IST to 00:30 IST (midnight rollover)
    rule = SessionScheduleRule(
        session_id=session.id,
        day_of_week=target_day,
        start_time=time(23, 30),
        end_time=time(0, 30),
        timezone="Asia/Kolkata",
        effective_start_date=today_kolkata
    )
    db_session.add(rule)
    await db_session.commit()

    count = await SchedulingEngine.generate_occurrences_for_session(db_session, session.id, days_ahead=7)
    assert count >= 1

    stmt = select(ClassOccurrence).where(
        ClassOccurrence.session_id == session.id
    ).order_by(ClassOccurrence.scheduled_start_at)
    occurrences = (await db_session.execute(stmt)).scalars().all()
    first_occ = occurrences[0]

    # Verify start time in UTC: 23:30 IST is 18:00 UTC
    assert first_occ.scheduled_start_at.hour == 18
    assert first_occ.scheduled_start_at.minute == 30 - 30  # 18:00

    # Verify end time in UTC: 00:30 IST is 19:00 UTC (duration 60 mins)
    duration = (first_occ.scheduled_end_at - first_occ.scheduled_start_at).total_seconds()
    assert duration == 3600.0
    assert first_occ.scheduled_end_at.hour == 19
    assert first_occ.scheduled_end_at.minute == 0

@pytest.mark.asyncio
async def test_session_update_syncs_future_occurrences(db_session):
    """
    AUD-P1-1: Editing a class must preserve and synchronize assigned teacher to future occurrences.
    """
    org = Organization(name="Sync Shala", slug=f"sync-{uuid.uuid4().hex[:6]}", timezone="Asia/Kolkata")
    db_session.add(org)
    await db_session.flush()

    u_owner = User(email=f"owner-{uuid.uuid4().hex[:6]}@test.local", full_name="Owner", role="STUDIO")
    u_teacher1 = User(email=f"t1-{uuid.uuid4().hex[:6]}@test.local", full_name="Teacher One", role="TEACHER")
    u_teacher2 = User(email=f"t2-{uuid.uuid4().hex[:6]}@test.local", full_name="Teacher Two", role="TEACHER")
    db_session.add_all([u_owner, u_teacher1, u_teacher2])
    await db_session.flush()

    m_owner = OrganizationMember(organization_id=org.id, user_id=u_owner.id, role="OWNER")
    m_teacher1 = OrganizationMember(organization_id=org.id, user_id=u_teacher1.id, role="TEACHER")
    m_teacher2 = OrganizationMember(organization_id=org.id, user_id=u_teacher2.id, role="TEACHER")
    db_session.add_all([m_owner, m_teacher1, m_teacher2])
    await db_session.flush()

    ct = ClassType(organization_id=org.id, name="Vinyasa", slug=f"vin-{uuid.uuid4().hex[:6]}")
    db_session.add(ct)
    await db_session.flush()

    session = Session(
        organization_id=org.id,
        class_type_id=ct.id,
        name="Dynamic Flow",
        slug=f"dyn-{uuid.uuid4().hex[:6]}",
        session_type=SessionType.GROUP.value,
        duration_minutes=60,
        is_active=True
    )
    db_session.add(session)
    await db_session.flush()

    # Assign Teacher 1 initially
    db_session.add(SessionInstructor(session_id=session.id, member_id=m_teacher1.id, role="PRIMARY"))

    # Future occurrence assigned to Teacher 1
    future_start = datetime.now(timezone.utc) + timedelta(days=2)
    occ = ClassOccurrence(
        session_id=session.id,
        organization_id=org.id,
        instructor_id=m_teacher1.id,
        scheduled_start_at=future_start,
        scheduled_end_at=future_start + timedelta(hours=1),
        actual_start_at=future_start,
        actual_end_at=future_start + timedelta(hours=1),
        status=OccurrenceStatus.SCHEDULED.value
    )
    db_session.add(occ)
    await db_session.commit()

    # Update session instructor to Teacher 2
    payload = SessionUpdate(
        name="Dynamic Flow Reassigned",
        instructor_member_ids=[m_teacher2.id]
    )
    await update_session(
        org_id=org.id,
        class_id=session.id,
        payload=payload,
        tenant=(org, m_owner),
        db=db_session
    )

    await db_session.refresh(occ)
    # Future occurrence must have its instructor updated to Teacher 2
    assert occ.instructor_id == m_teacher2.id

@pytest.mark.asyncio
async def test_student_meeting_links_without_time_window(client: AsyncClient, db_session, auth_headers):
    """
    AUD-P0-2 & AUD-P1-2: Server computes can_join and eligibility_reason.
    Meeting actions remain available before the class without a time window.
    """
    org = Organization(name="Eligibility Studio", slug=f"el-{uuid.uuid4().hex[:6]}", timezone="Asia/Kolkata")
    db_session.add(org)
    await db_session.flush()

    student = User(email=f"st-{uuid.uuid4().hex[:6]}@test.local", full_name="Student Test", role="STUDENT", is_verified=True, account_status="ACTIVE")
    teacher = User(email=f"tc-{uuid.uuid4().hex[:6]}@test.local", full_name="Teacher Test", role="TEACHER", is_verified=True, account_status="ACTIVE")
    db_session.add_all([student, teacher])
    await db_session.flush()

    m_teacher = OrganizationMember(organization_id=org.id, user_id=teacher.id, role="TEACHER")
    db_session.add(m_teacher)
    await db_session.flush()

    ct = ClassType(organization_id=org.id, name="Pranayama", slug=f"pr-{uuid.uuid4().hex[:6]}")
    db_session.add(ct)
    await db_session.flush()

    session = Session(
        organization_id=org.id,
        class_type_id=ct.id,
        name="Breath Reset",
        slug=f"breath-{uuid.uuid4().hex[:6]}",
        session_type=SessionType.GROUP.value,
        duration_minutes=45,
        is_active=True
    )
    db_session.add(session)
    await db_session.flush()

    now = datetime.now(timezone.utc)

    # 1. Occurrence too far in future (>15m)
    future_start = now + timedelta(hours=2)
    occ_far = ClassOccurrence(
        session_id=session.id,
        organization_id=org.id,
        instructor_id=m_teacher.id,
        scheduled_start_at=future_start,
        scheduled_end_at=future_start + timedelta(minutes=45),
        actual_start_at=future_start,
        actual_end_at=future_start + timedelta(minutes=45),
        status=OccurrenceStatus.SCHEDULED.value,
        meeting_url="https://zoom.us/j/111222333"
    )

    # 2. Occurrence ready to join (5 mins before start)
    ready_start = now + timedelta(minutes=5)
    occ_ready = ClassOccurrence(
        session_id=session.id,
        organization_id=org.id,
        instructor_id=m_teacher.id,
        scheduled_start_at=ready_start,
        scheduled_end_at=ready_start + timedelta(minutes=45),
        actual_start_at=ready_start,
        actual_end_at=ready_start + timedelta(minutes=45),
        status=OccurrenceStatus.SCHEDULED.value,
        meeting_url="https://zoom.us/j/444555666"
    )

    # 3. Active enrollment for student
    enr = SessionEnrollment(
        organization_id=org.id,
        session_id=session.id,
        user_id=student.id,
        status=EnrollmentStatus.ACTIVE.value,
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=30),
        total_sessions=10,
        sessions_attended=1
    )
    db_session.add_all([occ_far, occ_ready, enr])
    await db_session.commit()

    h = auth_headers(student)

    # Fetch upcoming classes
    resp = await client.get("student/upcoming-classes", headers=h)
    assert resp.status_code == 200
    classes = resp.json()

    far_item = next((c for c in classes if c["id"] == str(occ_far.id)), None)
    assert far_item is not None
    assert far_item["can_join"] is True
    assert far_item["eligibility_reason"] == "Ready to join"

    ready_item = next((c for c in classes if c["id"] == str(occ_ready.id)), None)
    assert ready_item is not None
    assert ready_item["can_join"] is True
    assert ready_item["eligibility_reason"] == "Ready to join"

    # Fetch meeting URL for ready class
    meet_resp = await client.get(f"student/classes/{occ_ready.id}/meeting-url", headers=h)
    assert meet_resp.status_code == 200
    assert meet_resp.json()["meeting_url"] == "https://zoom.us/j/444555666"

    # Future classes are accessible before the former 15-minute window.
    far_resp = await client.get(f"student/classes/{occ_far.id}/meeting-url", headers=h)
    assert far_resp.status_code == 200
    assert far_resp.json()["meeting_url"] == "https://zoom.us/j/111222333"

@pytest.mark.asyncio
async def test_support_ticket_conversation_flow(client: AsyncClient, db_session, auth_headers):
    """
    AUD-P1-5: Persisted student/owner support queries and resolution history.
    """
    org = Organization(name="Support Studio", slug=f"sup-{uuid.uuid4().hex[:6]}", timezone="Asia/Kolkata")
    db_session.add(org)
    await db_session.flush()

    owner = User(email=f"sup-owner-{uuid.uuid4().hex[:6]}@test.local", full_name="Studio Owner", role="STUDIO", is_verified=True, account_status="ACTIVE")
    student = User(email=f"sup-student-{uuid.uuid4().hex[:6]}@test.local", full_name="Ananya Student", role="STUDENT", is_verified=True, account_status="ACTIVE")
    db_session.add_all([owner, student])
    await db_session.flush()

    m_owner = OrganizationMember(organization_id=org.id, user_id=owner.id, role="STUDIO", is_active=True)
    db_session.add_all([m_owner, OrganizationStudent(organization_id=org.id, user_id=student.id)])
    await db_session.commit()

    student_h = auth_headers(student)
    owner_h = auth_headers(owner)

    # 1. Student creates support ticket
    create_resp = await client.post("student/support-tickets", headers=student_h, json={
        "organization_id": str(org.id),
        "subject": "Question regarding Aerial Silks rig height",
        "initial_message": "Hi, what ceiling height is recommended for the home suspension silks?",
        "category": "GENERAL"
    })
    assert create_resp.status_code == 201
    ticket_data = create_resp.json()
    ticket_id = ticket_data["id"]
    assert ticket_data["subject"] == "Question regarding Aerial Silks rig height"
    assert ticket_data["status"] == "OPEN"

    # 2. Owner fetches organization support tickets
    list_resp = await client.get(f"organizations/{org.id}/support-tickets", headers=owner_h)
    assert list_resp.status_code == 200
    tickets = list_resp.json()
    assert any(t["id"] == ticket_id for t in tickets)

    # 3. Owner replies to ticket
    reply_resp = await client.post(f"organizations/{org.id}/support-tickets/{ticket_id}/messages", headers=owner_h, json={
        "message": "We recommend a minimum ceiling clearance of 9.5 feet with structural beam support."
    })
    assert reply_resp.status_code == 200
    assert reply_resp.json()["sender_name"] == "Studio Owner"

    # 4. Owner updates ticket status to RESOLVED
    status_resp = await client.patch(f"organizations/{org.id}/support-tickets/{ticket_id}/status", headers=owner_h, json={
        "status": "RESOLVED"
    })
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "RESOLVED"

    # 5. Student fetches ticket details and verifies message history
    detail_resp = await client.get(f"student/support-tickets/{ticket_id}", headers=student_h)
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["status"] == "RESOLVED"
    assert len(detail["messages"]) == 2  # initial + owner reply
    assert "minimum ceiling clearance" in detail["messages"][1]["message"]

@pytest.mark.asyncio
async def test_password_recovery_journey(client: AsyncClient, db_session):
    """
    AUD-P2-8: Complete password recovery flow with OTP request and reset.
    """
    email = f"reset-{uuid.uuid4().hex[:6]}@test.local"
    old_pw = "OldPassword123"
    new_pw = "NewSecurePassword456!"

    # Create user
    user = User(
        email=email,
        full_name="Password Reset User",
        role="STUDENT",
        is_verified=True,
        account_status="ACTIVE"
    )
    db_session.add(user)
    await db_session.flush()

    from app.core.security import get_password_hash
    from app.models.otp import UserOtp
    from app.modules.auth.service import _hash_string

    user.password_hash = get_password_hash(old_pw)
    await db_session.commit()

    # 1. Request forgot password OTP
    forgot_resp = await client.post("auth/forgot-password", json={"email": email})
    assert forgot_resp.status_code == 200
    forgot_data = forgot_resp.json()
    assert "message" in forgot_data

    # Retrieve OTP directly from database to test verification
    otp_record = await db_session.scalar(
        select(UserOtp).where(UserOtp.email == email).order_by(UserOtp.created_at.desc())
    )
    assert otp_record is not None
    # Let's seed a known OTP to test reset-password verification
    test_otp = "852963"
    otp_record.otp_hash = _hash_string(test_otp)
    await db_session.commit()

    # 2. Reset password with OTP
    reset_resp = await client.post("auth/reset-password", json={
        "email": email,
        "code": test_otp,
        "new_password": new_pw
    })
    assert reset_resp.status_code == 200
    reset_data = reset_resp.json()
    assert reset_data["success"] is True

    # 3. Verify user can log in with new password
    login_resp = await client.post("auth/login", json={
        "email": email,
        "password": new_pw
    })
    assert login_resp.status_code == 200
    assert "access_token" in login_resp.json()


@pytest.mark.asyncio
async def test_public_session_buying_information(client, db_session):
    """
    AUD-P0-5: Public class detail must return venue, next actual date, availability,
    renewal/cancellation terms, and meeting readiness before purchase.
    """
    org = Organization(name="Kevala Shala", slug=f"kevala-{uuid.uuid4().hex[:6]}", timezone="Asia/Kolkata")
    db_session.add(org)
    await db_session.flush()

    org_setting = OrganizationSetting(
        organization_id=org.id,
        teaching_mode="HYBRID",
        address_line1="42 Lotus Path",
        city="Bengaluru",
        state="Karnataka",
        pincode="560001"
    )
    db_session.add(org_setting)
    await db_session.flush()

    ct = ClassType(organization_id=org.id, name="Vinyasa Yoga", slug=f"vinyasa-{uuid.uuid4().hex[:6]}")
    db_session.add(ct)
    await db_session.flush()

    session = Session(
        organization_id=org.id,
        class_type_id=ct.id,
        name="Prana Vinyasa Flow",
        slug=f"prana-vinyasa-{uuid.uuid4().hex[:6]}",
        session_type=SessionType.GROUP.value,
        duration_minutes=60,
        capacity=20,
        is_active=True,
        is_public=True
    )
    db_session.add(session)
    await db_session.flush()

    pricing = SessionPricing(
        session_id=session.id,
        pricing_type="PER_SESSION",
        price=500.0,
        currency="INR",
        validity_days=30
    )
    db_session.add(pricing)

    # Add an upcoming occurrence
    future_start = datetime.now(timezone.utc) + timedelta(days=2)
    occ = ClassOccurrence(
        session_id=session.id,
        organization_id=org.id,
        scheduled_start_at=future_start,
        scheduled_end_at=future_start + timedelta(minutes=60),
        actual_start_at=future_start,
        actual_end_at=future_start + timedelta(minutes=60),
        status="SCHEDULED"
    )
    db_session.add(occ)
    await db_session.commit()

    resp = await client.get(f"public/organizations/{org.slug}/classes/{session.slug}")
    assert resp.status_code == 200
    data = resp.json()

    assert data["name"] == "Prana Vinyasa Flow"
    assert "delivery_mode" in data
    assert data["venue"]["city"] == "Bengaluru"
    assert "42 Lotus Path" in data["venue"]["address"]
    assert data["next_occurrence"] is not None
    assert data["next_occurrence"]["id"] == str(occ.id)
    assert data["availability"]["capacity"] == 20
    assert data["availability"]["remaining_spots"] == 20
    assert "at least 2 hours before class start" in data["cancellation_terms"]
    assert "Unused sessions remain available within the pass validity" in data["cancellation_terms"]
    assert data["purchase_policy"]["version"]
    assert data["purchase_policy"]["terms"] == data["cancellation_terms"]
    assert "refunds_supported" not in data["purchase_policy"]
    assert "refund" not in data["purchase_policy"]["terms"].lower()
    assert "Zoom video room" in data["meeting_readiness"]
    assert "No auto-recurring" in data["renewal_terms"]


@pytest.mark.asyncio
async def test_timezone_repair_historical_and_scheduled(db_session):
    """
    AUD-P0-4: Timezone repair script safely fixes shifted occurrences across both
    SCHEDULED and historical COMPLETED states without altering attendance records.
    """
    from app.modules.scheduling.repair import execute_repair
    from app.models.schedule import ClassOccurrenceChange

    org = Organization(name="Yin Sanctuary", slug=f"yin-{uuid.uuid4().hex[:6]}", timezone="Asia/Kolkata")
    db_session.add(org)
    await db_session.flush()

    ct = ClassType(organization_id=org.id, name="Yin Yoga", slug=f"yin-{uuid.uuid4().hex[:6]}")
    db_session.add(ct)
    await db_session.flush()

    session = Session(
        organization_id=org.id,
        class_type_id=ct.id,
        name="Yin Deep Fascia",
        slug=f"yin-deep-{uuid.uuid4().hex[:6]}",
        session_type=SessionType.GROUP.value,
        duration_minutes=60,
        is_active=True
    )
    db_session.add(session)
    await db_session.flush()

    rule = SessionScheduleRule(
        session_id=session.id,
        day_of_week=2,  # Tuesday
        start_time=time(19, 0),
        end_time=time(20, 0),
        timezone="Asia/Kolkata",
        effective_start_date=date.today(),
        is_active=True
    )
    db_session.add(rule)
    await db_session.flush()

    # Seed an occurrence where UTC time was mistakenly saved with rule's local time (19:00 UTC instead of 13:30 UTC)
    # Stored 19:00 UTC in Asia/Kolkata displays as 00:30 next day (the bug)
    buggy_utc = datetime(2026, 10, 14, 19, 0, 0, tzinfo=timezone.utc)
    occ = ClassOccurrence(
        session_id=session.id,
        organization_id=org.id,
        scheduled_start_at=buggy_utc,
        scheduled_end_at=buggy_utc + timedelta(hours=1),
        actual_start_at=buggy_utc,
        actual_end_at=buggy_utc + timedelta(hours=1),
        status="COMPLETED"
    )
    db_session.add(occ)
    await db_session.commit()

    # Execute repair
    summary = await execute_repair(db_session, dry_run=False)
    assert summary["repaired_count"] >= 1

    await db_session.refresh(occ)
    kolkata_tz = ZoneInfo("Asia/Kolkata")
    utc_start = occ.actual_start_at if occ.actual_start_at.tzinfo else occ.actual_start_at.replace(tzinfo=timezone.utc)
    local_start = utc_start.astimezone(kolkata_tz)

    # Must be shifted to 13:30 UTC = 19:00 IST on Tuesday Oct 14
    assert occ.actual_start_at.hour == 13
    assert occ.actual_start_at.minute == 30
    assert local_start.hour == 19
    assert local_start.minute == 0
    assert local_start.day == 14

    # Verify audit change was logged
    change = (await db_session.execute(
        select(ClassOccurrenceChange).where(ClassOccurrenceChange.class_occurrence_id == occ.id)
    )).scalar_one_or_none()
    assert change is not None
    assert change.change_type == "TIMEZONE_REPAIR"

