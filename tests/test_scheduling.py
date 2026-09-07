import pytest
import uuid
from datetime import datetime, time, timedelta, timezone
from app.models.organization import Organization, OrganizationMember
from app.models.session import ClassType, Session, SessionInstructor
from app.models.schedule import SessionScheduleRule, ClassOccurrence
from app.models.user import User
from app.modules.scheduling.engine import SchedulingEngine
from app.common.enums import OccurrenceStatus

@pytest.mark.asyncio
async def test_scheduling_occurrence_generation(db_session):
    now = datetime.now(timezone.utc)
    org = Organization(name="Test Studio", slug="test-studio-sched")
    db_session.add(org)
    await db_session.flush()

    user = User(email="inst@test.local", full_name="Instructor One")
    db_session.add(user)
    await db_session.flush()

    member = OrganizationMember(organization_id=org.id, user_id=user.id, role="INSTRUCTOR")
    db_session.add(member)

    ct = ClassType(organization_id=org.id, name="Fitness", slug="fitness")
    db_session.add(ct)
    await db_session.flush()

    session = Session(
        organization_id=org.id,
        class_type_id=ct.id,
        name="Bootcamp",
        slug="bootcamp"
    )
    db_session.add(session)
    await db_session.flush()

    db_session.add(SessionInstructor(session_id=session.id, member_id=member.id, role="PRIMARY"))

    # Add schedule rule for today's weekday
    today_weekday = now.date().weekday()
    rule = SessionScheduleRule(
        session_id=session.id,
        day_of_week=today_weekday,
        start_time=time(10, 0),
        end_time=time(11, 0),
        effective_start_date=now.date(),
        effective_end_date=now.date() + timedelta(days=14)
    )
    db_session.add(rule)
    await db_session.commit()

    # Generate occurrences
    count = await SchedulingEngine.generate_occurrences_for_session(db_session, session.id, days_ahead=14)
    assert count >= 2 # At least 2 or 3 weeks of occurrences generated

@pytest.mark.asyncio
async def test_instructor_conflict_detection(db_session):
    now = datetime.now(timezone.utc)
    start_at = now + timedelta(days=1)
    end_at = start_at + timedelta(hours=1)

    org = Organization(name="Conflict Studio", slug="conflict-studio")
    db_session.add(org)
    await db_session.flush()

    user = User(email="conf.inst@test.local", full_name="Busy Instructor")
    db_session.add(user)
    await db_session.flush()

    member = OrganizationMember(organization_id=org.id, user_id=user.id, role="INSTRUCTOR")
    db_session.add(member)
    await db_session.flush()

    # Existing occurrence
    occ = ClassOccurrence(
        session_id=uuid.uuid4(),
        organization_id=org.id,
        instructor_id=member.id,
        scheduled_start_at=start_at,
        scheduled_end_at=end_at,
        actual_start_at=start_at,
        actual_end_at=end_at,
        status=OccurrenceStatus.SCHEDULED.value
    )
    db_session.add(occ)
    await db_session.commit()

    # Check conflict with overlapping window
    has_conflict = await SchedulingEngine.check_instructor_conflict(
        db_session,
        instructor_id=member.id,
        start_at=start_at + timedelta(minutes=30),
        end_at=end_at + timedelta(minutes=30)
    )
    assert has_conflict is True

    # Check non-overlapping window
    no_conflict = await SchedulingEngine.check_instructor_conflict(
        db_session,
        instructor_id=member.id,
        start_at=end_at + timedelta(hours=2),
        end_at=end_at + timedelta(hours=3)
    )
    assert no_conflict is False
