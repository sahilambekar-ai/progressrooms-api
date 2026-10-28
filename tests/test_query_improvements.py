"""Regression coverage for bounded scheduling and attendance queries."""
import uuid
from datetime import datetime, time, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, func
from app.models.organization import Organization
from app.models.user import User
from app.models.session import ClassType, Session
from app.models.schedule import ClassOccurrence, SessionScheduleRule
from app.models.commerce import SessionEnrollment
from app.models.attendance import AttendanceRecord
from app.modules.scheduling.engine import SchedulingEngine
from app.modules.attendance.router import get_attendance_roster


async def make_class(db):
    org = Organization(name='Query regression', slug=uuid.uuid4().hex)
    db.add(org)
    await db.flush()
    kind = ClassType(organization_id=org.id, name='Yoga', slug=uuid.uuid4().hex)
    db.add(kind)
    await db.flush()
    cls = Session(organization_id=org.id, class_type_id=kind.id, name='Yoga', slug=uuid.uuid4().hex)
    db.add(cls)
    await db.flush()
    return org, cls


@pytest.mark.asyncio
@pytest.mark.parametrize("zoom_mode,query_budget", [("disabled", 4), ("mock", 5)])
async def test_generation_is_bounded_idempotent_and_refreshes_rules(db_session, monkeypatch, zoom_mode, query_budget):
    from cryptography.fernet import Fernet
    from app.core.config import settings
    monkeypatch.setattr(settings, "APP_ENV", "test")
    monkeypatch.setattr(settings, "ZOOM_PROVIDER_MODE", zoom_mode)
    monkeypatch.setattr(settings, "ZOOM_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    org, cls = await make_class(db_session)
    today = datetime.now(timezone.utc).date()
    for _ in range(2):  # Overlapping rules must not duplicate occurrences within one batch.
        db_session.add(SessionScheduleRule(session_id=cls.id, day_of_week=today.weekday(),
            start_time=time(10), end_time=time(11), effective_start_date=today))
    await db_session.flush()
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    event.listen(db_session.bind.sync_engine, 'before_cursor_execute', record)
    try:
        count = await SchedulingEngine.generate_occurrences_for_session(db_session, cls.id, 70)
        assert count == 11
        assert len(statements) == query_budget  # One additional settings read when Zoom is enabled.
        statements.clear()
        assert await SchedulingEngine.generate_occurrences_for_session(db_session, cls.id, 70) == 0
        assert len(statements) == query_budget
        # The request session has already loaded the old rules. Replacement must refresh it.
        for rule in list(cls.schedule_rules):
            await db_session.delete(rule)
        db_session.add(SessionScheduleRule(session_id=cls.id, day_of_week=today.weekday(),
            start_time=time(12), end_time=time(13), effective_start_date=today))
        assert await SchedulingEngine.generate_occurrences_for_session(db_session, cls.id, 0) == 1
    finally:
        event.remove(db_session.bind.sync_engine, 'before_cursor_execute', record)
    assert await db_session.scalar(select(func.count()).select_from(ClassOccurrence).where(
        ClassOccurrence.session_id == cls.id)) == 12


@pytest.mark.asyncio
async def test_conflict_with_multiple_matches(db_session):
    org, cls = await make_class(db_session)
    now = datetime.now(timezone.utc)
    instructor_id = uuid.uuid4()
    for offset in (0, 10):
        start = now + timedelta(minutes=offset)
        db_session.add(ClassOccurrence(session_id=cls.id, organization_id=org.id,
            instructor_id=instructor_id, scheduled_start_at=start,
            scheduled_end_at=start + timedelta(hours=1), actual_start_at=start,
            actual_end_at=start + timedelta(hours=1)))
    await db_session.flush()
    assert await SchedulingEngine.check_instructor_conflict(
        db_session, instructor_id, now + timedelta(minutes=20), now + timedelta(minutes=30))


@pytest.mark.asyncio
async def test_roster_only_reads_requested_attendance(db_session, monkeypatch):
    org, cls = await make_class(db_session)
    now = datetime.now(timezone.utc)
    occurrences = [ClassOccurrence(session_id=cls.id, organization_id=org.id,
        scheduled_start_at=now + timedelta(days=i), scheduled_end_at=now + timedelta(days=i, hours=1),
        actual_start_at=now + timedelta(days=i), actual_end_at=now + timedelta(days=i, hours=1)) for i in range(2)]
    users = [User(email=f'{uuid.uuid4().hex}@example.com', full_name=f'Learner {i}') for i in range(2)]
    db_session.add_all(occurrences + users)
    await db_session.flush()
    enrollments = [SessionEnrollment(organization_id=org.id, session_id=cls.id, user_id=u.id,
        valid_from=now, sessions_attended=0) for u in users]
    db_session.add_all(enrollments)
    await db_session.flush()
    db_session.add_all([
        AttendanceRecord(class_occurrence_id=occurrences[0].id, session_enrollment_id=enrollments[0].id, status='ABSENT'),
        AttendanceRecord(class_occurrence_id=occurrences[1].id, session_enrollment_id=enrollments[0].id, status='ATTENDED'),
    ])
    await db_session.flush()
    monkeypatch.setattr('app.modules.attendance.router.auto_complete_expired_one_on_one_classes', AsyncMock())
    statements = []
    def record(*args): statements.append(args[2])
    event.listen(db_session.bind.sync_engine, 'before_cursor_execute', record)
    try:
        rows = await get_attendance_roster(org.id, occurrences[0].id, (org, None), db_session)
    finally:
        event.remove(db_session.bind.sync_engine, 'before_cursor_execute', record)
    assert len(statements) == 2  # authorized occurrence + joined roster
    assert {row['student_name']: row['attendance_status'] for row in rows} == {
        'Learner 0': 'ABSENT', 'Learner 1': 'ATTENDED'}


@pytest.mark.asyncio
async def test_otp_request_does_not_store_plaintext_mirror(db_session, monkeypatch):
    from app.models.otp import UserOtp
    from app.modules.auth.service import AuthService, _hash_string
    user = User(email=f'{uuid.uuid4().hex}@example.com', full_name='OTP learner')
    db_session.add(user)
    await db_session.flush()
    monkeypatch.setattr('app.modules.auth.service.send_otp_email', AsyncMock())
    code, record_id, *_ = await AuthService.request_otp(db_session, user.email)
    record = await db_session.get(UserOtp, uuid.UUID(record_id))
    assert record.otp_hash == _hash_string(code)
    assert "user_auth_methods" not in User.metadata.tables
    token, verified = await AuthService.verify_otp(db_session, user.email, code)
    assert token and verified.is_verified


@pytest.mark.asyncio
async def test_upcoming_classes_deduplicate_enrollments(db_session):
    from app.modules.student.router import get_my_upcoming_classes
    org, cls = await make_class(db_session)
    user = User(email=f'{uuid.uuid4().hex}@example.com', full_name='Schedule learner')
    db_session.add(user)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    for _ in range(2):
        db_session.add(SessionEnrollment(organization_id=org.id, session_id=cls.id,
            user_id=user.id, valid_from=now, status='ACTIVE'))
    start = now + timedelta(days=1)
    occ = ClassOccurrence(session_id=cls.id, organization_id=org.id,
        scheduled_start_at=start, scheduled_end_at=start + timedelta(hours=1),
        actual_start_at=start, actual_end_at=start + timedelta(hours=1))
    db_session.add(occ)
    await db_session.flush()
    rows = await get_my_upcoming_classes(user, db_session)
    assert [row['id'] for row in rows] == [str(occ.id)]
    outsider = User(id=uuid.uuid4(), email='outsider@example.com', full_name='Outsider')
    assert await get_my_upcoming_classes(outsider, db_session) == []


@pytest.mark.asyncio
async def test_future_appointment_visible_before_enrollment_starts_but_only_within_validity(db_session):
    from app.modules.student.router import get_my_upcoming_classes
    org, cls = await make_class(db_session)
    user = User(email=f'{uuid.uuid4().hex}@example.com', full_name='Future appointment student')
    db_session.add(user)
    await db_session.flush()
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    db_session.add(SessionEnrollment(organization_id=org.id, session_id=cls.id,
        user_id=user.id, valid_from=start, valid_until=start + timedelta(hours=2), status='ACTIVE'))
    occurrences = []
    for delta in (-1, 0, 3):
        scheduled = start + timedelta(hours=delta)
        occ = ClassOccurrence(session_id=cls.id, organization_id=org.id,
            scheduled_start_at=scheduled, actual_start_at=scheduled,
            scheduled_end_at=scheduled + timedelta(minutes=30), actual_end_at=scheduled + timedelta(minutes=30))
        db_session.add(occ)
        occurrences.append(occ)
    await db_session.flush()
    rows = await get_my_upcoming_classes(user, db_session)
    assert [row['id'] for row in rows] == [str(occurrences[1].id)]
