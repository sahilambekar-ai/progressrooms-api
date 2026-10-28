"""Sandbox payment acceptance run:
Validates: payment -> enrollment -> teacher roster -> student next class -> attendance -> credit deduction -> cancellation/reschedule.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import select

from app.main import app, api_v1
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token, get_password_hash
from app.models.user import User
from app.models.organization import Organization, OrganizationMember
from app.models.session import ClassType, Session, SessionPricing
from app.models.schedule import ClassOccurrence
from app.models.commerce import Order, SessionEnrollment, Payment
from app.models.attendance import AttendanceRecord
from app.common.enums import OrderStatus, PaymentStatus, AttendanceStatus


@pytest_asyncio.fixture
async def client(db_session):
    async def dependency():
        yield db_session
    api_v1.dependency_overrides[get_db] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1/") as client:
        yield client
    api_v1.dependency_overrides.clear()


def auth_header(user: User):
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}


@pytest.mark.asyncio
async def test_sandbox_payment_acceptance_flow(client, db_session, monkeypatch):
    """End-to-end sandbox acceptance run validating payment -> enrollment -> teacher roster -> student upcoming -> attendance -> credit deduction."""
    # Configure the sandbox explicitly; CI must not depend on a local .env.
    monkeypatch.setattr(settings, "DEV_TEST_LOGIN_ENABLED", True)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_mock_progressrooms")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "rzp_secret_mock_progressrooms")
    # 1. Setup Studio Organization and Owner
    owner_user = User(
        email=f"owner_{uuid.uuid4().hex[:8]}@studio.test",
        full_name="Studio Owner",
        role="STUDIO",
        phone="+91 98765 00001",
        password_hash=get_password_hash("password123"),
        is_verified=True,
        account_status="ACTIVE"
    )
    db_session.add(owner_user)
    await db_session.flush()

    org = Organization(name="Sandbox Yoga Sanctuary", slug=f"sandbox-yoga-{uuid.uuid4().hex[:8]}", timezone="UTC", currency="INR")
    db_session.add(org)
    await db_session.flush()

    owner_member = OrganizationMember(organization_id=org.id, user_id=owner_user.id, role="STUDIO")
    db_session.add(owner_member)

    # Setup Teacher
    teacher_user = User(
        email=f"teacher_{uuid.uuid4().hex[:8]}@studio.test",
        full_name="Aarav Sharma",
        role="TEACHER",
        phone="+91 98765 00002",
        password_hash=get_password_hash("password123"),
        is_verified=True,
        account_status="ACTIVE"
    )
    db_session.add(teacher_user)
    await db_session.flush()

    teacher_member = OrganizationMember(
        organization_id=org.id,
        user_id=teacher_user.id,
        role="TEACHER",
        teacher_code=f"TCH{uuid.uuid4().hex[:4].upper()}"
    )
    db_session.add(teacher_member)

    # Setup Class Type and Session
    class_type = ClassType(organization_id=org.id, name="Vinyasa Flow", slug=f"vinyasa-{uuid.uuid4().hex[:6]}")
    db_session.add(class_type)
    await db_session.flush()

    session_obj = Session(
        organization_id=org.id,
        class_type_id=class_type.id,
        name="Vinyasa Flow 5-Class Pack",
        slug=f"vinyasa-pack-{uuid.uuid4().hex[:6]}",
        capacity=20,
        is_active=True
    )
    db_session.add(session_obj)
    await db_session.flush()

    pricing = SessionPricing(
        session_id=session_obj.id,
        pricing_type="PACK",
        price=Decimal("1500.00"),
        currency="INR",
        total_classes=5,
        validity_days=30
    )
    db_session.add(pricing)

    # Setup upcoming class occurrence (tomorrow at 10 AM UTC)
    now = datetime.now(timezone.utc)
    occ_start = now + timedelta(days=1)
    occ_end = occ_start + timedelta(hours=1)
    occurrence = ClassOccurrence(
        organization_id=org.id,
        session_id=session_obj.id,
        instructor_id=teacher_member.id,
        scheduled_start_at=occ_start,
        scheduled_end_at=occ_end,
        actual_start_at=occ_start,
        actual_end_at=occ_end,
        status="SCHEDULED",
        meeting_url="https://zoom.us/j/999888777",
        host_url="https://zoom.us/s/999888777"
    )
    db_session.add(occurrence)

    # Setup 2nd future occurrence (4 days from now) for rescheduling test
    occ2_start = now + timedelta(days=4)
    occ2_end = occ2_start + timedelta(hours=1)
    occurrence2 = ClassOccurrence(
        organization_id=org.id,
        session_id=session_obj.id,
        instructor_id=teacher_member.id,
        scheduled_start_at=occ2_start,
        scheduled_end_at=occ2_end,
        actual_start_at=occ2_start,
        actual_end_at=occ2_end,
        status="SCHEDULED",
        meeting_url="https://zoom.us/j/999888778",
        host_url="https://zoom.us/s/999888778"
    )
    db_session.add(occurrence2)

    # Setup 3rd future occurrence (7 days from now) for rescheduling target
    occ3_start = now + timedelta(days=7)
    occ3_end = occ3_start + timedelta(hours=1)
    occurrence3 = ClassOccurrence(
        organization_id=org.id,
        session_id=session_obj.id,
        instructor_id=teacher_member.id,
        scheduled_start_at=occ3_start,
        scheduled_end_at=occ3_end,
        actual_start_at=occ3_start,
        actual_end_at=occ3_end,
        status="SCHEDULED",
        meeting_url="https://zoom.us/j/999888779",
        host_url="https://zoom.us/s/999888779"
    )
    db_session.add(occurrence3)

    # Setup Student
    student_user = User(
        email=f"student_{uuid.uuid4().hex[:8]}@example.com",
        full_name="Priya Patel",
        role="STUDENT",
        phone="+91 98765 00003",
        password_hash=get_password_hash("password123"),
        is_verified=True,
        account_status="ACTIVE"
    )
    db_session.add(student_user)
    await db_session.commit()

    student_auth = auth_header(student_user)
    owner_auth = auth_header(owner_user)

    # -------------------------------------------------------------
    # Step 1: Student creates order via POST /commerce/orders
    # -------------------------------------------------------------
    order_res = await client.post(
        "/commerce/orders",
        headers=student_auth,
        json={"session_id": str(session_obj.id)}
    )
    assert order_res.status_code == 200, order_res.text
    order_data = order_res.json()
    order_id = order_data["order_id"]
    assert order_data["amount"] == 1500.0
    assert order_data["currency"] == "INR"
    assert order_data["mock"] is True

    # -------------------------------------------------------------
    # Step 2: Student verifies payment via POST /commerce/payments/verify
    # -------------------------------------------------------------
    verify_res = await client.post(
        "/commerce/payments/verify",
        headers=student_auth,
        json={
            "order_id": order_id,
            "razorpay_order_id": order_data["razorpay_order_id"],
            "razorpay_payment_id": f"dev_{uuid.UUID(order_id).hex}",
            "razorpay_signature": "development-only"
        }
    )
    assert verify_res.status_code == 200, verify_res.text
    verify_data = verify_res.json()
    assert verify_data["success"] is True
    enrollment_id = verify_data["enrollment_id"]

    # -------------------------------------------------------------
    # Step 3: Check student's active enrollments
    # -------------------------------------------------------------
    enr_res = await client.get("/student/enrollments", headers=student_auth)
    assert enr_res.status_code == 200, enr_res.text
    enrollments = enr_res.json()
    assert len(enrollments) >= 1
    my_enr = next((e for e in enrollments if e["id"] == enrollment_id), None)
    assert my_enr is not None
    assert my_enr["status"] == "ACTIVE"
    assert my_enr["total_sessions"] == 5
    assert my_enr["sessions_attended"] == 0
    assert my_enr["sessions_restored"] == 0

    # -------------------------------------------------------------
    # Step 4: Verify student appears on Teacher/Studio Roster
    # -------------------------------------------------------------
    roster_res = await client.get(
        f"/organizations/{org.id}/occurrences/{occurrence.id}/attendance",
        headers=owner_auth
    )
    assert roster_res.status_code == 200, roster_res.text
    roster = roster_res.json()
    roster_student = next((r for r in roster if r["student_id"] == str(student_user.id)), None)
    assert roster_student is not None
    assert roster_student["student_name"] == "Priya Patel"
    assert roster_student["attendance_status"] == "SCHEDULED"
    assert roster_student["sessions_attended"] == 0
    assert roster_student["total_sessions"] == 5

    # -------------------------------------------------------------
    # Step 5: Verify class appears in student's upcoming classes
    # -------------------------------------------------------------
    upcoming_res = await client.get("/student/upcoming-classes", headers=student_auth)
    assert upcoming_res.status_code == 200, upcoming_res.text
    upcoming = upcoming_res.json()
    upcoming_occ = next((c for c in upcoming if c["id"] == str(occurrence.id)), None)
    assert upcoming_occ is not None
    assert upcoming_occ["session_name"] == "Vinyasa Flow 5-Class Pack"

    # -------------------------------------------------------------
    # Step 6: Teacher marks attendance as ATTENDED -> credit deduction
    # -------------------------------------------------------------
    mark_res = await client.post(
        f"/organizations/{org.id}/occurrences/{occurrence.id}/attendance",
        headers=owner_auth,
        json={
            "enrollment_id": enrollment_id,
            "status": "ATTENDED",
            "notes": "Great practice today"
        }
    )
    assert mark_res.status_code == 200, mark_res.text

    # Verify credit deduction on roster
    roster_after = (await client.get(
        f"/organizations/{org.id}/occurrences/{occurrence.id}/attendance",
        headers=owner_auth
    )).json()
    student_after = next(r for r in roster_after if r["student_id"] == str(student_user.id))
    assert student_after["attendance_status"] == "ATTENDED"
    assert student_after["sessions_attended"] == 1

    # Verify credit deduction on student enrollment
    enr_after = (await client.get("/student/enrollments", headers=student_auth)).json()
    my_enr_after = next(e for e in enr_after if e["id"] == enrollment_id)
    assert my_enr_after["sessions_attended"] == 1

    # -------------------------------------------------------------
    # Step 7: Reschedule / Cancel Policy:
    # Student checks available reschedule slots for occurrence2 (>2hr in future)
    # -------------------------------------------------------------
    slots_res = await client.get(
        f"/student/classes/{occurrence2.id}/available-reschedule-slots",
        headers=student_auth
    )
    assert slots_res.status_code == 200, slots_res.text
    slots = slots_res.json()
    assert any(s["id"] == str(occurrence3.id) for s in slots)

    # Student reschedules occurrence2 to occurrence3
    reschedule_res = await client.post(
        f"/student/classes/{occurrence2.id}/reschedule",
        headers=student_auth,
        json={"target_occurrence_id": str(occurrence3.id)}
    )
    assert reschedule_res.status_code == 200, reschedule_res.text
    reschedule_data = reschedule_res.json()
    assert reschedule_data["target_occurrence_id"] == str(occurrence3.id)

    # Student cancels occurrence3 (>2hr before start) -> credit restored
    cancel_res = await client.post(
        f"/student/classes/{occurrence3.id}/cancel",
        headers=student_auth
    )
    assert cancel_res.status_code == 200, cancel_res.text
    cancel_data = cancel_res.json()
    assert cancel_data["credit_preserved"] is True
    assert "refunded" not in cancel_data
    assert "No payment is returned" in cancel_data["message"]
    assert "unused session remains available" in cancel_data["message"]

    # Verify occurrence3 is no longer in student's upcoming classes (it is EXCUSED)
    upcoming_after_cancel = (await client.get("/student/upcoming-classes", headers=student_auth)).json()
    assert not any(c["id"] == str(occurrence3.id) for c in upcoming_after_cancel)

    # Verify enrollment shows 2 restored credits from cancellations/reschedules
    enr_restored = (await client.get("/student/enrollments", headers=student_auth)).json()
    my_enr_restored = next(e for e in enr_restored if e["id"] == enrollment_id)
    assert my_enr_restored["sessions_restored"] == 2
    assert my_enr_restored["sessions_attended"] == 1
