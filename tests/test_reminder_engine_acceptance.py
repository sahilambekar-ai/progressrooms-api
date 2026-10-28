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


from app.models.communication import MeetingEmailDelivery, UserCommunicationPreference
from app.common.dates import ensure_utc
from app.modules.communication.reminder_engine import request_meeting_invites, backfill_reminders
from app.modules.communication.worker import deliver_one
from app.modules.zoom.provider import ProviderError
from fastapi import HTTPException
from types import SimpleNamespace


async def make_occurrence(db):
    org, teacher, member, student, session = await create_test_ecosystem(db)
    start = datetime.now(timezone.utc) + timedelta(hours=2)
    occ = ClassOccurrence(session_id=session.id, organization_id=org.id, instructor_id=member.id,
        scheduled_start_at=start, scheduled_end_at=start+timedelta(hours=1),
        actual_start_at=start, actual_end_at=start+timedelta(hours=1), status="SCHEDULED")
    db.add(occ)
    await db.commit()
    return occ, teacher, student


@pytest.mark.asyncio
async def test_only_one_reminder_at_30_minutes_and_no_early_dispatch(db_session):
    occ, teacher, student = await make_occurrence(db_session)
    jobs = await sync_reminder_schedules_for_occurrence(db_session, occ.id, occ.actual_start_at)
    assert len(jobs) == 1
    assert jobs[0].reminder_type == "REMINDER_30M"
    due = ensure_utc(occ.actual_start_at) - timedelta(minutes=30)
    assert ensure_utc(jobs[0].target_time) == due
    assert await dispatch_pending_reminders(db_session, due-timedelta(seconds=1)) == 0
    assert await dispatch_pending_reminders(db_session, due) == 2
    assert await dispatch_pending_reminders(db_session, due+timedelta(seconds=1)) == 0
    deliveries = (await db_session.scalars(select(MeetingEmailDelivery))).all()
    assert {row.recipient_user_id for row in deliveries} == {teacher.id, student.id}
    assert all(row.status == "PENDING" for row in deliveries)
    assert not (await db_session.scalars(select(CommunicationLog).where(CommunicationLog.status == "SENT"))).all()


@pytest.mark.asyncio
async def test_late_booking_gets_one_immediate_reminder_and_no_backlog(db_session):
    occ, _, _ = await make_occurrence(db_session)
    now = datetime.now(timezone.utc)
    jobs = await sync_reminder_schedules_for_occurrence(db_session, occ.id, now+timedelta(minutes=10), current_time=now)
    assert len(jobs) == 1 and ensure_utc(jobs[0].target_time) == now
    assert await sync_reminder_schedules_for_occurrence(db_session, occ.id, now-timedelta(seconds=1), current_time=now) == []


@pytest.mark.asyncio
async def test_backfill_is_idempotent(db_session):
    occ, _, _ = await make_occurrence(db_session)
    now = datetime.now(timezone.utc)
    await backfill_reminders(db_session, now)
    await backfill_reminders(db_session, now)
    assert len((await db_session.scalars(select(ClassReminderSchedule).where(ClassReminderSchedule.class_occurrence_id == occ.id))).all()) == 1


@pytest.mark.asyncio
async def test_reschedule_supersedes_old_queue_and_consolidates_all_changes(db_session):
    occ, _, _ = await make_occurrence(db_session)
    now = datetime.now(timezone.utc)
    old = occ.actual_start_at
    await sync_reminder_schedules_for_occurrence(db_session, occ.id, old)
    await dispatch_pending_reminders(db_session, old-timedelta(minutes=30))
    occ.actual_start_at = old + timedelta(hours=1)
    await record_class_edit(db_session, occ.id, {"actual_start_at": occ.actual_start_at}, {"actual_start_at": old}, current_time=now)
    await record_class_edit(db_session, occ.id, {"name": "Updated"}, {}, current_time=now+timedelta(seconds=30))
    assert all(row.status == "SUPERSEDED" for row in (await db_session.scalars(select(MeetingEmailDelivery))).all())
    assert await process_debounced_material_corrections(db_session, now+timedelta(seconds=119)) == 0
    assert await process_debounced_material_corrections(db_session, now+timedelta(seconds=121)) == 2
    assert await process_debounced_material_corrections(db_session, now+timedelta(seconds=122)) == 0


@pytest.mark.asyncio
async def test_cancellation_stops_reminders_and_enqueues_once(db_session):
    occ, _, _ = await make_occurrence(db_session)
    await sync_reminder_schedules_for_occurrence(db_session, occ.id, occ.actual_start_at)
    occ.status = "CANCELLED"
    assert await handle_class_cancellation(db_session, occ.id, "Teacher unavailable") == 2
    assert await handle_class_cancellation(db_session, occ.id, "Teacher unavailable") == 0
    jobs = (await db_session.scalars(select(ClassReminderSchedule))).all()
    assert all(job.status == "CANCELLED" for job in jobs)
    rows = (await db_session.scalars(select(MeetingEmailDelivery))).all()
    assert all(row.message_type == "CLASS_CANCELLATION" for row in rows)


@pytest.mark.asyncio
async def test_manual_invites_staff_all_student_self_and_cooldown(db_session, monkeypatch):
    occ, teacher, student = await make_occurrence(db_session)
    async def host(*args): return SimpleNamespace(mode="mock")
    monkeypatch.setattr("app.modules.zoom.meetings.host_for_occurrence", host)
    result = await request_meeting_invites(db_session, occ, recipient_id=student.id)
    assert result["recipients"] == 1
    assert (await request_meeting_invites(db_session, occ, recipient_id=student.id))["recipients"] == 1
    assert len((await db_session.scalars(select(MeetingEmailDelivery))).all()) == 1
    assert (await request_meeting_invites(db_session, occ))["recipients"] == 1
    assert len((await db_session.scalars(select(MeetingEmailDelivery))).all()) == 2
    with pytest.raises(HTTPException) as error:
        await request_meeting_invites(db_session, occ, recipient_id=uuid.uuid4())
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_expired_pass_is_not_emailed(db_session):
    occ, teacher, student = await make_occurrence(db_session)
    enrollment = await db_session.scalar(select(SessionEnrollment).where(SessionEnrollment.user_id == student.id))
    enrollment.valid_until = occ.actual_start_at-timedelta(minutes=1)
    await db_session.flush()
    await sync_reminder_schedules_for_occurrence(db_session, occ.id, occ.actual_start_at)
    assert await dispatch_pending_reminders(db_session, occ.actual_start_at-timedelta(minutes=30)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,expected", [("success", "SENT"), ("simulation", "SIMULATED"), ("timeout", "UNCERTAIN"), ("rate_limit", "RETRY"), ("denied", "FAILED"), ("optout", "SUPPRESSED")])
async def test_worker_records_real_provider_result_and_prevents_duplicate_claims(db_session, monkeypatch, outcome, expected):
    occ, _, student = await make_occurrence(db_session)
    await sync_reminder_schedules_for_occurrence(db_session, occ.id, occ.actual_start_at)
    await dispatch_pending_reminders(db_session, occ.actual_start_at-timedelta(minutes=30))
    row = await db_session.scalar(select(MeetingEmailDelivery).where(MeetingEmailDelivery.recipient_user_id == student.id))
    row.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    calls = []
    async def host(*args): return SimpleNamespace(mode="mock")
    async def send(*args):
        calls.append(args)
        if outcome == "timeout": raise ProviderError(ambiguous=True)
        if outcome == "rate_limit": raise ProviderError(status_code=429, retry_after=120)
        if outcome == "denied": raise ProviderError("Mailbox not provisioned")
        return {"id": "zoom-id", "simulated": outcome == "simulation"}
    monkeypatch.setattr("app.modules.communication.worker.host_for_occurrence", host)
    monkeypatch.setattr("app.modules.communication.worker.send_zoom_email", send)
    if outcome == "optout":
        db_session.add(UserCommunicationPreference(user_id=student.id, email_opted_out=True))
    await db_session.commit()
    await deliver_one(db_session, row.id)
    await db_session.refresh(row)
    assert row.status == expected
    assert len(calls) == (0 if outcome == "optout" else 1)
    await deliver_one(db_session, row.id)
    assert len(calls) == (0 if outcome == "optout" else 1)
    logs = (await db_session.scalars(select(CommunicationLog).where(CommunicationLog.channel == "ZOOM_MAIL"))).all()
    assert len(logs) == 1 and logs[0].status == expected
    if calls:
        assert "start_url" not in calls[0][4] and f"/meetings/{occ.id}?" in calls[0][4]


@pytest.mark.asyncio
async def test_changed_assignment_or_cancelled_class_blocks_queued_invite(db_session, monkeypatch):
    occ, _, student = await make_occurrence(db_session)
    await sync_reminder_schedules_for_occurrence(db_session, occ.id, occ.actual_start_at)
    await dispatch_pending_reminders(db_session, occ.actual_start_at-timedelta(minutes=30))
    row = await db_session.scalar(select(MeetingEmailDelivery).where(MeetingEmailDelivery.recipient_user_id == student.id))
    row.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    occ.status = "CANCELLED"
    await db_session.commit()
    await deliver_one(db_session, row.id)
    await db_session.refresh(row)
    assert row.status == "CANCELLED"


@pytest.mark.asyncio
async def test_validation_worker_only_processes_its_selected_studio(db_session, monkeypatch):
    from contextlib import asynccontextmanager
    from app.core.config import settings
    from app.modules.communication.worker import run_cycle
    first, _, _ = await make_occurrence(db_session)
    second, _, _ = await make_occurrence(db_session)
    now = datetime.now(timezone.utc)
    first.actual_start_at = second.actual_start_at = now + timedelta(minutes=10)
    await db_session.flush()
    await sync_reminder_schedules_for_occurrence(db_session, first.id, first.actual_start_at, current_time=now)
    await sync_reminder_schedules_for_occurrence(db_session, second.id, second.actual_start_at, current_time=now)
    await dispatch_pending_reminders(db_session, now, organization_id=first.organization_id)
    assert {row.class_occurrence_id for row in (await db_session.scalars(select(MeetingEmailDelivery))).all()} == {first.id}
    second_job = await db_session.scalar(select(ClassReminderSchedule).where(ClassReminderSchedule.class_occurrence_id == second.id))
    assert second_job.status == 'PENDING'
    await dispatch_pending_reminders(db_session, now, organization_id=second.organization_id)
    await db_session.commit()
    called = []
    async def fake_delivery(db, delivery_id): called.append(delivery_id)
    @asynccontextmanager
    async def factory(): yield db_session
    monkeypatch.setattr(settings, 'MEETING_EMAIL_ORGANIZATION_ID', first.organization_id)
    monkeypatch.setattr('app.modules.communication.worker.deliver_one', fake_delivery)
    count = await run_cycle(factory)
    expected = set((await db_session.scalars(select(MeetingEmailDelivery.id).where(MeetingEmailDelivery.class_occurrence_id == first.id))).all())
    assert count == len(expected) == 2
    assert set(called) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'short_description': 'Bring a mat'},
    {'requirements': {'required_equipment': 'Yoga mat'}},
    {'capacity': 12},
    {'is_public': False},
    {'pricing': {'price': 100}},
])
async def test_class_edits_queue_change_emails_for_teacher_and_student(db_session, changes):
    from app.modules.classes.router import update_session
    from app.modules.classes.schemas import SessionUpdate
    occ, teacher, student = await make_occurrence(db_session)
    org = await db_session.get(Organization, occ.organization_id)
    await update_session(org.id, occ.session_id, SessionUpdate(**changes), (org, None), db_session)
    change = await db_session.scalar(select(PendingMaterialChange).where(PendingMaterialChange.class_occurrence_id == occ.id))
    assert set(change.changes_summary) == set(changes)
    assert await process_debounced_material_corrections(db_session, datetime.now(timezone.utc) + timedelta(minutes=3)) == 2
    rows = (await db_session.scalars(select(MeetingEmailDelivery))).all()
    assert {row.recipient_user_id for row in rows} == {teacher.id, student.id}
    assert all(row.message_type == 'CLASS_CORRECTION' for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize('via_toggle', [True, False])
async def test_pause_email_is_delivered_without_join_link_and_resume_is_queued(db_session, monkeypatch, via_toggle):
    from app.modules.classes.router import update_session, toggle_session_status
    from app.modules.classes.schemas import SessionUpdate, SessionToggleStatus
    occ, _, student = await make_occurrence(db_session)
    org = await db_session.get(Organization, occ.organization_id)
    if via_toggle:
        await toggle_session_status(org.id, occ.session_id, SessionToggleStatus(is_active=False), (org, None), db_session)
    else:
        await update_session(org.id, occ.session_id, SessionUpdate(is_active=False), (org, None), db_session)
    await process_debounced_material_corrections(db_session, datetime.now(timezone.utc) + timedelta(minutes=3))
    row = await db_session.scalar(select(MeetingEmailDelivery).where(MeetingEmailDelivery.recipient_user_id == student.id))
    row.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    calls = []
    async def host(*args): return SimpleNamespace(mode='mock')
    async def send(*args):
        calls.append(args)
        return {'id': 'simulated-pause', 'simulated': True}
    monkeypatch.setattr('app.modules.communication.worker.host_for_occurrence', host)
    monkeypatch.setattr('app.modules.communication.worker.send_zoom_email', send)
    await db_session.commit()
    await deliver_one(db_session, row.id)
    await db_session.refresh(row)
    assert row.status == 'SIMULATED'
    assert len(calls) == 1
    assert 'Paused' in calls[0][4]
    assert '/meetings/' not in calls[0][4]
    await toggle_session_status(org.id, occ.session_id, SessionToggleStatus(is_active=True), (org, None), db_session)
    assert await process_debounced_material_corrections(db_session, datetime.now(timezone.utc) + timedelta(minutes=3)) == 2
