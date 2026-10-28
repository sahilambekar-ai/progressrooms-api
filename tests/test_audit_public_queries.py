"""Check public catalog enrollment counts and their query growth."""
from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import event

from app.models.organization import Organization
from app.models.user import User
from app.models.session import ClassType, Session, SessionPricing
from app.models.commerce import SessionEnrollment
from app.modules.websites.router import get_public_organization_website


@pytest.mark.asyncio
@pytest.mark.parametrize("class_count", [1, 6])
async def test_public_enrollment_counts_are_batched(db_session, class_count):
    org = Organization(name="Catalog", slug=uuid.uuid4().hex)
    user = User(email=f"{uuid.uuid4().hex}@example.com", full_name="Learner")
    db_session.add_all([org, user])
    await db_session.flush()
    kind = ClassType(organization_id=org.id, name="Yoga", slug="yoga")
    db_session.add(kind)
    await db_session.flush()
    classes = [Session(organization_id=org.id, class_type_id=kind.id, name=f"Class {i}",
        slug=f"class-{i}", capacity=10) for i in range(class_count)]
    db_session.add_all(classes)
    await db_session.flush()
    for cls in classes:
        db_session.add(SessionPricing(session_id=cls.id, price=0))
    for status in ["ACTIVE", "ACTIVE", "EXPIRED"]:
        db_session.add(SessionEnrollment(organization_id=org.id, session_id=classes[0].id,
            user_id=user.id, status=status, valid_from=datetime.now(timezone.utc)))
    await db_session.flush()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "count(session_enrollments.id)" in statement.lower():
            statements.append(statement)

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", record)
    try:
        result = await get_public_organization_website(org.slug, db_session)
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", record)
    assert len(result["sessions"]) == class_count
    remaining = {row["slug"]: row["remaining_spots"] for row in result["sessions"]}
    assert remaining["class-0"] == 8
    assert all(remaining[f"class-{i}"] == 10 for i in range(1, class_count))
    assert len(statements) == 1, f"{class_count} classes required {len(statements)} enrollment queries"


@pytest.mark.asyncio
@pytest.mark.parametrize('class_count',[1,8])
async def test_public_teacher_catalog_batches_enrollments_and_next_occurrences(db_session,class_count):
    from datetime import timedelta
    from app.models.organization import OrganizationMember
    from app.models.session import SessionInstructor
    from app.models.schedule import ClassOccurrence
    from app.modules.teachers.router import public_teacher_lookup
    from test_account_roles import staff
    teacher,org,member=await staff(db_session,'TEACHER')
    member.teacher_code='13245678'
    kind=ClassType(organization_id=org.id,name='Yoga',slug='yoga');db_session.add(kind);await db_session.flush()
    now=datetime.now(timezone.utc)
    for i in range(class_count):
        course=Session(organization_id=org.id,class_type_id=kind.id,name=f'Course {i}',slug=f'course-{i}',capacity=10)
        db_session.add(course);await db_session.flush()
        db_session.add(SessionInstructor(session_id=course.id,member_id=member.id))
        db_session.add(SessionEnrollment(organization_id=org.id,session_id=course.id,user_id=teacher.id,status='ACTIVE',valid_from=now))
        for offset in [1,2]:
            start=now+timedelta(days=offset)
            db_session.add(ClassOccurrence(organization_id=org.id,session_id=course.id,actual_start_at=start,actual_end_at=start+timedelta(hours=1),scheduled_start_at=start,scheduled_end_at=start+timedelta(hours=1)))
    await db_session.commit()
    statements=[]
    def record(conn,cursor,statement,parameters,context,executemany):
        if 'count(session_enrollments.id)' in statement.lower() or ('row_number()' in statement.lower() and 'class_occurrences' in statement.lower()):statements.append(statement)
    event.listen(db_session.bind.sync_engine,'before_cursor_execute',record)
    try:result=await public_teacher_lookup(member.teacher_code,db_session)
    finally:event.remove(db_session.bind.sync_engine,'before_cursor_execute',record)
    classes=result['studios'][0]['classes'];assert len(classes)==class_count
    assert all(c['remaining_spots']==9 for c in classes)
    assert all(c['next_session']['start_time'].startswith((now+timedelta(days=1)).date().isoformat()) for c in classes)
    assert len(statements)==2
