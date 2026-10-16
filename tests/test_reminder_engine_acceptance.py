import pytest
import pytest_asyncio
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select

from app.models.user import User
from app.models.organization import Organization, OrganizationMember
from app.models.session import ClassType, Session, SessionInstructor, SessionPricing
from app.models.schedule import ClassOccurrence
from app.models.commerce import SessionEnrollment
from app.models.communication import (
    PlatformCommunicationSetting,
    ClassReminderSchedule,
    PendingMaterialChange,
    CommunicationLog,
)
from app.modules.communication.reminder_engine import (
    sync_reminder_schedules_for_occurrence,
    record_class_edit,
    handle_class_cancellation,
    dispatch_pending_reminders,
    process_debounced_material_corrections,
    format_ist_time,
    calculate_suggested_join_time,
)
from app.modules.communication.service import get_or_create_settings


@pytest.fixture
def ist_tz():
    return ZoneInfo("Asia/Kolkata")


async def create_test_ecosystem(db_session):
    """
    Creates a studio, teacher, enrolled student, and a scheduled batch session.
    """
    org = Organization(name="Prana Sanctuary", slug=f"prana-{uuid.uuid4().hex[:6]}", timezone="Asia/Kolkata")
    db_session.add(org)
    await db_session.flush()

    # Teacher user & member
    teacher_user = User(
        email=f"teacher_{uuid.uuid4().hex[:6]}@prana.test",
        full_name="Aarohi Sharma",
        phone="+91 98765 11223",
        whatsapp_number="+91 98765 11223",
        role="TEACHER",
        is_active=True,
        is_verified=True,
    )
    db_session.add(teacher_user)
    await db_session.flush()

    teacher_member = OrganizationMember(
        organization_id=org.id,
        user_id=teacher_user.id,
        role="TEACHER"
    )
    db_session.add(teacher_member)
    await db_session.flush()

    # Student user
    student_user = User(
        email=f"student_{uuid.uuid4().hex[:6]}@example.com",
        full_name="Rohan Verma",
        phone="+91 98765 44556",
        whatsapp_number="+91 98765 44556",
        role="STUDENT",
        location="Mumbai",
        is_active=True,
        is_verified=True,
    )
    db_session.add(student_user)
    await db_session.flush()

    # Class Type & Session
    class_type = ClassType(organization_id=org.id, name="Hatha Flow", slug=f"hatha-{uuid.uuid4().hex[:6]}")
    db_session.add(class_type)
    await db_session.flush()

    session = Session(
        organization_id=org.id,
        class_type_id=class_type.id,
        name="Morning Prana Flow",
        slug=f"morning-prana-{uuid.uuid4().hex[:6]}",
        duration_minutes=60,
        is_active=True,
        is_public=True
    )
    db_session.add(session)
    await db_session.flush()

    si = SessionInstructor(session_id=session.id, member_id=teacher_member.id, role="PRIMARY")
    db_session.add(si)

    # Active Student Enrollment
    now_utc = datetime.now(timezone.utc)
    enrollment = SessionEnrollment(
        organization_id=org.id,
        session_id=session.id,
        user_id=student_user.id,
        status="ACTIVE",
        valid_from=now_utc - timedelta(days=1),
        valid_until=now_utc + timedelta(days=30),
        total_sessions=12,
        sessions_attended=2,
        billing_value=2500.0,
        billing_currency="INR"
    )
    db_session.add(enrollment)
    # Ensure platform switches default to True for acceptance testing
    platform_settings = await get_or_create_settings(db_session)
    platform_settings.whatsapp_enabled = True
    platform_settings.email_enabled = True
    await db_session.commit()

    return org, teacher_user, teacher_member, student_user, session



@pytest.mark.asyncio
async def test_ten_edits_before_two_hour_reminder_produce_one_accurate_reminder(db_session, ist_tz):
    """
    CRITICAL ACCEPTANCE SCENARIO 1:
    Class scheduled for 9:00 AM IST.
    Owner makes 10 edits before 7:00 AM IST (routine edits: name, duration, description, teacher, slight time tweaks).
    - Expected: 0 spam notifications sent during the 10 edits.
    - At 7:00 AM IST (T-2h), exactly 1 consolidated preparation reminder is dispatched with final details.
    - Both reminders ask the student to join 5 minutes early (8:55 AM IST).
    - Never exposes host meeting URL; provides secure student portal link.
    """
    org, teacher_user, teacher_member, student_user, session = await create_test_ecosystem(db_session)

    # Schedule class for tomorrow at 9:00 AM IST
    # 9:00 AM IST = 03:30 AM UTC
    tomorrow = (datetime.now(ist_tz) + timedelta(days=1)).date()
    start_ist = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, 0, 0, tzinfo=ist_tz)
    start_utc = start_ist.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(minutes=60)

    occ = ClassOccurrence(
        session_id=session.id,
        organization_id=org.id,
        instructor_id=teacher_member.id,
        scheduled_start_at=start_utc,
        scheduled_end_at=end_utc,
        actual_start_at=start_utc,
        actual_end_at=end_utc,
        status="SCHEDULED",
        meeting_url="https://meet.jit.si/secret-host-room-never-leak"
    )
    db_session.add(occ)
    await db_session.commit()

    # Initial reminder jobs sync at creation (say at 5:00 AM IST = T-4h)
    time_5am_ist = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 5, 0, 0, tzinfo=ist_tz).astimezone(timezone.utc)
    await sync_reminder_schedules_for_occurrence(
        db=db_session,
        occurrence_id=occ.id,
        new_start_at=start_utc,
        current_time=time_5am_ist
    )
    await db_session.commit()

    # Verify 2 pending jobs created (2h and 15m)
    jobs = (await db_session.scalars(
        select(ClassReminderSchedule).where(ClassReminderSchedule.class_occurrence_id == occ.id)
    )).all()
    assert len(jobs) == 2
    assert {j.reminder_type for j in jobs} == {"REMINDER_2H", "REMINDER_15M"}

    # Simulate 10 sequential edits before 7:00 AM IST
    time_cursor = time_5am_ist + timedelta(minutes=5)
    for edit_num in range(1, 11):
        time_cursor += timedelta(minutes=5)
        edit_result = await record_class_edit(
            db=db_session,
            occurrence_id=occ.id,
            changed_fields={
                "short_description": f"Edit #{edit_num}: Bringing awareness to spine and breath.",
                "duration_minutes": 60 + (edit_num % 15),
            },
            old_values={"duration_minutes": 60},
            current_time=time_cursor
        )
        assert edit_result["action"] == "SILENT_UPDATE"
        assert edit_result["notifications_sent"] == 0

    # Verify 0 communication log entries exist after 10 edits
    logs_after_10_edits = (await db_session.scalars(
        select(CommunicationLog).where(CommunicationLog.recipient_user_id == student_user.id)
    )).all()
    assert len(logs_after_10_edits) == 0, "No notifications should be sent to students during pre-2h edits!"

    # Now time reaches 7:00 AM IST (T-2h exactly: 01:30 AM UTC)
    time_7am_ist = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 7, 0, 0, tzinfo=ist_tz).astimezone(timezone.utc)
    dispatched_count = await dispatch_pending_reminders(db=db_session, as_of_time=time_7am_ist, occurrence_id=occ.id)

    # Dispatched to student (WhatsApp + Email) + teacher (WhatsApp + Email)
    assert dispatched_count > 0

    # Inspect student's 2-hour reminder log
    student_logs = (await db_session.scalars(
        select(CommunicationLog).where(
            CommunicationLog.recipient_user_id == student_user.id,
            CommunicationLog.message_type == "REMINDER_2H"
        )
    )).all()

    # Must be exactly 1 per enabled channel
    wa_log = next((l for l in student_logs if l.channel == "WHATSAPP_CLASSES"), None)
    email_log = next((l for l in student_logs if l.channel == "EMAIL"), None)

    assert wa_log is not None
    assert wa_log.status == "SENT"
    assert email_log is not None
    assert email_log.status == "SENT"

    # Verify content asks to join 5 minutes early
    assert "08:55 AM IST" in wa_log.body_preview or "5 minutes early" in wa_log.body_preview
    assert "secret-host-room-never-leak" not in (wa_log.body_preview or "")


@pytest.mark.asyncio
async def test_material_changes_after_reminder_produce_consolidated_correction(db_session, ist_tz):
    """
    CRITICAL ACCEPTANCE SCENARIO 2:
    After the 2-hour reminder has been sent, owner makes multiple material edits
    in quick succession (e.g. within 2 minutes: shifts start time and changes teacher).
    - Expected: Edits are consolidated into a 2-minute debounce window.
    - Exactly 1 consolidated correction notification is sent (not 2 separate messages).
    """
    org, teacher_user, teacher_member, student_user, session = await create_test_ecosystem(db_session)

    tomorrow = (datetime.now(ist_tz) + timedelta(days=1)).date()
    start_ist = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, 0, 0, tzinfo=ist_tz)
    start_utc = start_ist.astimezone(timezone.utc)

    occ = ClassOccurrence(
        session_id=session.id,
        organization_id=org.id,
        instructor_id=teacher_member.id,
        scheduled_start_at=start_utc,
        scheduled_end_at=start_utc + timedelta(minutes=60),
        actual_start_at=start_utc,
        actual_end_at=start_utc + timedelta(minutes=60),
        status="SCHEDULED"
    )
    db_session.add(occ)
    await db_session.commit()

    # Mark 2-hour reminder as already SENT at 7:00 AM IST
    job_2h = ClassReminderSchedule(
        class_occurrence_id=occ.id,
        reminder_type="REMINDER_2H",
        target_time=start_utc - timedelta(hours=2),
        status="SENT",
        sent_at=start_utc - timedelta(hours=2)
    )
    db_session.add(job_2h)
    await db_session.commit()

    # At 7:15 AM IST, owner makes Material Edit 1: Start time moved to 9:30 AM IST
    time_715_ist = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 7, 15, 0, tzinfo=ist_tz).astimezone(timezone.utc)
    new_start_utc = start_utc + timedelta(minutes=30)
    occ.actual_start_at = new_start_utc

    res1 = await record_class_edit(
        db=db_session,
        occurrence_id=occ.id,
        changed_fields={"actual_start_at": new_start_utc},
        old_values={"actual_start_at": start_utc},
        current_time=time_715_ist
    )
    assert res1["action"] == "CORRECTION_QUEUED"
    await db_session.commit()

    # 30 seconds later (7:15:30 AM IST), owner makes Material Edit 2: Teacher changed
    time_715_30_ist = time_715_ist + timedelta(seconds=30)
    res2 = await record_class_edit(
        db=db_session,
        occurrence_id=occ.id,
        changed_fields={"instructor_id": teacher_member.id},
        old_values={"instructor_id": None},
        current_time=time_715_30_ist
    )
    assert res2["action"] == "CORRECTION_QUEUED"
    await db_session.commit()

    # Check that at 7:16 AM IST (1 minute in, debounce not yet expired), 0 corrections sent
    corrections_early = await process_debounced_material_corrections(
        db=db_session,
        as_of_time=time_715_ist + timedelta(seconds=60),
        occurrence_id=occ.id
    )
    assert corrections_early == 0, "Debounce window of 2 minutes must hold dispatches"

    # Now time reaches 7:17:30 AM IST (2.5 minutes later, debounce expired)
    time_717_30_ist = time_715_ist + timedelta(minutes=2, seconds=30)
    corrections_sent = await process_debounced_material_corrections(
        db=db_session,
        as_of_time=time_717_30_ist,
        occurrence_id=occ.id
    )
    assert corrections_sent > 0

    # Verify student received exactly 1 consolidated correction
    student_corrections = (await db_session.scalars(
        select(CommunicationLog).where(
            CommunicationLog.recipient_user_id == student_user.id,
            CommunicationLog.message_type == "CLASS_CORRECTION"
        )
    )).all()

    # 1 WhatsApp and 1 Email
    assert len(student_corrections) == 2
    statuses = [c.status for c in student_corrections]
    assert all(s == "SENT" for s in statuses)


@pytest.mark.asyncio
async def test_cancellation_prevents_remaining_reminders_and_alerts_promptly(db_session, ist_tz):
    """
    CRITICAL ACCEPTANCE SCENARIO 3:
    Class is cancelled.
    - All pending reminders are immediately marked CANCELLED.
    - Cancellation notification is dispatched promptly to student & teacher.
    - When future reminder times arrive (e.g. 15-minute mark), 0 reminders are sent.
    """
    org, teacher_user, teacher_member, student_user, session = await create_test_ecosystem(db_session)

    tomorrow = (datetime.now(ist_tz) + timedelta(days=1)).date()
    start_ist = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, 0, 0, tzinfo=ist_tz)
    start_utc = start_ist.astimezone(timezone.utc)

    occ = ClassOccurrence(
        session_id=session.id,
        organization_id=org.id,
        instructor_id=teacher_member.id,
        scheduled_start_at=start_utc,
        scheduled_end_at=start_utc + timedelta(minutes=60),
        actual_start_at=start_utc,
        actual_end_at=start_utc + timedelta(minutes=60),
        status="SCHEDULED"
    )
    db_session.add(occ)
    await db_session.commit()

    # 2-hour reminder already sent
    job_2h = ClassReminderSchedule(
        class_occurrence_id=occ.id,
        reminder_type="REMINDER_2H",
        target_time=start_utc - timedelta(hours=2),
        status="SENT",
        sent_at=start_utc - timedelta(hours=2)
    )
    # 15-minute reminder still pending
    job_15m = ClassReminderSchedule(
        class_occurrence_id=occ.id,
        reminder_type="REMINDER_15M",
        target_time=start_utc - timedelta(minutes=15),
        status="PENDING"
    )
    db_session.add_all([job_2h, job_15m])
    await db_session.commit()

    # At 7:45 AM IST, owner cancels the class
    time_cancel_ist = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 7, 45, 0, tzinfo=ist_tz).astimezone(timezone.utc)
    occ.status = "CANCELLED"
    await handle_class_cancellation(
        db=db_session,
        occurrence_id=occ.id,
        reason="Teacher emergency",
        current_time=time_cancel_ist
    )

    # 1. Verify pending 15-minute job is now CANCELLED
    await db_session.refresh(job_15m)
    assert job_15m.status == "CANCELLED"

    # 2. Verify student received immediate cancellation notice
    cancellation_logs = (await db_session.scalars(
        select(CommunicationLog).where(
            CommunicationLog.recipient_user_id == student_user.id,
            CommunicationLog.message_type == "CLASS_CANCELLATION"
        )
    )).all()
    assert len(cancellation_logs) >= 1
    assert any("Teacher emergency" in (l.body_preview or "") or l.metadata_json.get("reason") == "Teacher emergency" for l in cancellation_logs)

    # 3. Time advances to 8:45 AM IST (T-15m). Run dispatch_pending_reminders.
    time_845_ist = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 8, 45, 0, tzinfo=ist_tz).astimezone(timezone.utc)
    dispatched_at_15m = await dispatch_pending_reminders(db=db_session, as_of_time=time_845_ist, occurrence_id=occ.id)

    # Zero reminders sent because the job was cancelled!
    reminders_15m_sent = (await db_session.scalars(
        select(CommunicationLog).where(
            CommunicationLog.recipient_user_id == student_user.id,
            CommunicationLog.message_type == "REMINDER_15M"
        )
    )).all()
    assert len(reminders_15m_sent) == 0, "Cancelled class must never send 15m reminder"


@pytest.mark.asyncio
async def test_class_created_within_two_hours_and_fifteen_minutes(db_session, ist_tz):
    """
    SECTION 2 SPECIFICATION:
    - If created within 2 hours: send one current-details notice immediately, then the 15-minute reminder if still useful.
    - If created within 15 minutes: send one starting-soon notice immediately.
    - Never send a backlog of missed reminders.
    """
    org, teacher_user, teacher_member, student_user, session = await create_test_ecosystem(db_session)
    now_utc = datetime.now(timezone.utc)

    # 1. Class created 1 hour from now (within 2 hours)
    start_in_1h = now_utc + timedelta(hours=1)
    occ_1h = ClassOccurrence(
        session_id=session.id,
        organization_id=org.id,
        instructor_id=teacher_member.id,
        scheduled_start_at=start_in_1h,
        scheduled_end_at=start_in_1h + timedelta(minutes=60),
        actual_start_at=start_in_1h,
        actual_end_at=start_in_1h + timedelta(minutes=60),
        status="SCHEDULED"
    )
    db_session.add(occ_1h)
    await db_session.commit()

    jobs_1h = await sync_reminder_schedules_for_occurrence(
        db=db_session,
        occurrence_id=occ_1h.id,
        new_start_at=start_in_1h,
        current_time=now_utc
    )
    # Expected: 1 immediate (2h prep) job due NOW + 1 job at T-15m
    assert len(jobs_1h) == 2
    prep_job = next(j for j in jobs_1h if j.reminder_type == "REMINDER_2H")
    fifteen_job = next(j for j in jobs_1h if j.reminder_type == "REMINDER_15M")
    assert prep_job.target_time == now_utc
    assert fifteen_job.target_time == start_in_1h - timedelta(minutes=15)

    # 2. Class created 10 minutes from now (within 15 minutes)
    start_in_10m = now_utc + timedelta(minutes=10)
    occ_10m = ClassOccurrence(
        session_id=session.id,
        organization_id=org.id,
        instructor_id=teacher_member.id,
        scheduled_start_at=start_in_10m,
        scheduled_end_at=start_in_10m + timedelta(minutes=60),
        actual_start_at=start_in_10m,
        actual_end_at=start_in_10m + timedelta(minutes=60),
        status="SCHEDULED"
    )
    db_session.add(occ_10m)
    await db_session.commit()

    jobs_10m = await sync_reminder_schedules_for_occurrence(
        db=db_session,
        occurrence_id=occ_10m.id,
        new_start_at=start_in_10m,
        current_time=now_utc
    )
    # Expected: Exactly 1 starting soon notice due NOW; NO backlog of 2h reminder
    assert len(jobs_10m) == 1
    assert jobs_10m[0].reminder_type == "REMINDER_15M"
    assert jobs_10m[0].target_time == now_utc


@pytest.mark.asyncio
async def test_channel_suppression_and_opt_out_enforcement(db_session, ist_tz):
    """
    Verifies that:
    1. Recipient opt-out suppresses sends on that channel with "Suppressed: recipient opt-out".
    2. Platform switch off suppresses sends with "Suppressed: email disabled" / "Suppressed: whatsapp disabled".
    """
    from app.models.communication import UserCommunicationPreference

    org, teacher_user, teacher_member, student_user, session = await create_test_ecosystem(db_session)
    now_utc = datetime.now(timezone.utc)
    start_in_3h = now_utc + timedelta(hours=3)

    occ = ClassOccurrence(
        session_id=session.id,
        organization_id=org.id,
        instructor_id=teacher_member.id,
        scheduled_start_at=start_in_3h,
        scheduled_end_at=start_in_3h + timedelta(minutes=60),
        actual_start_at=start_in_3h,
        actual_end_at=start_in_3h + timedelta(minutes=60),
        status="SCHEDULED"
    )
    db_session.add(occ)
    await db_session.commit()

    # Student opts out of WhatsApp
    pref = UserCommunicationPreference(
        user_id=student_user.id,
        whatsapp_opted_out=True,
        email_opted_out=False
    )
    db_session.add(pref)

    # Platform disables operational Email
    platform_settings = await get_or_create_settings(db_session)
    platform_settings.email_enabled = False
    await db_session.commit()

    # Schedule reminder jobs
    await sync_reminder_schedules_for_occurrence(
        db=db_session,
        occurrence_id=occ.id,
        new_start_at=start_in_3h,
        current_time=now_utc
    )
    await db_session.commit()

    # Time advances to T-2h: dispatch
    time_2h = start_in_3h - timedelta(hours=2)
    await dispatch_pending_reminders(db=db_session, as_of_time=time_2h, occurrence_id=occ.id)

    # Check logs for student
    student_logs = (await db_session.scalars(
        select(CommunicationLog).where(
            CommunicationLog.recipient_user_id == student_user.id,
            CommunicationLog.message_type == "REMINDER_2H"
        )
    )).all()

    wa_log = next(l for l in student_logs if l.channel == "WHATSAPP_CLASSES")
    email_log = next(l for l in student_logs if l.channel == "EMAIL")

    assert wa_log.status == "SUPPRESSED"
    assert wa_log.suppression_reason == "Suppressed: recipient opt-out"

    assert email_log.status == "SUPPRESSED"
    assert email_log.suppression_reason == "Suppressed: email disabled"

