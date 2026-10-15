"""Verify aggregate responses and query budgets without touching a live database."""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, func
from app.models.organization import Organization, OrganizationMember, OrganizationStudent
from app.models.user import User
from app.models.commerce import Order, SessionEnrollment
from app.models.plan import OrganizationSubscription
from app.models.session import ClassType, Session
from app.modules.admin.router import get_admin_metrics
from app.modules.students.router import list_students


@pytest.mark.asyncio
async def test_admin_metrics_matches_independent_aggregates(db_session):
    queries = [select(func.count(model.id)) for model in
               (Organization, OrganizationMember, OrganizationStudent, User)]
    queries += [select(func.sum(Order.total_amount)).where(Order.status == 'PAID'),
                select(func.count(OrganizationSubscription.id)).where(OrganizationSubscription.status == 'ACTIVE')]
    expected = [await db_session.scalar(query) or 0 for query in queries]
    statements = []
    def record(*args): statements.append(args[2])
    engine = db_session.bind.sync_engine
    event.listen(engine, 'before_cursor_execute', record)
    try:
        result = await get_admin_metrics(admin=None, db=db_session)
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert len(statements) == 1
    assert [result[key] for key in ('total_organizations', 'total_instructors', 'total_students',
                                   'total_users', 'total_platform_revenue', 'active_subscriptions')] == expected
    assert result['status'] == 'healthy'


@pytest.mark.asyncio
async def test_student_totals_are_batched_and_tenant_scoped(db_session, monkeypatch):
    orgs = [Organization(name='Query test', slug=uuid.uuid4().hex) for _ in range(2)]
    users = [User(email=f'{uuid.uuid4().hex}@example.com', full_name=f'Student {i}', role='STUDENT') for i in range(3)]
    db_session.add_all(orgs + users)
    await db_session.flush()
    classes = []
    for org in orgs:
        kind = ClassType(organization_id=org.id, name='Yoga', slug='yoga')
        db_session.add(kind)
        await db_session.flush()
        cls = Session(organization_id=org.id, class_type_id=kind.id, name='Yoga', slug='yoga')
        db_session.add(cls)
        classes.append(cls)
    await db_session.flush()
    db_session.add_all([OrganizationStudent(organization_id=orgs[0].id, user_id=u.id) for u in users])
    for org, cls, user, attended, total, status in [
        (orgs[0], classes[0], users[0], 2, 5, 'ACTIVE'),
        (orgs[0], classes[0], users[0], 3, None, 'EXPIRED'),
        (orgs[1], classes[1], users[0], 90, 100, 'ACTIVE'),
        (orgs[0], classes[0], users[2], 0, None, 'ACTIVE'),
    ]:
        db_session.add(SessionEnrollment(organization_id=org.id, session_id=cls.id, user_id=user.id,
                       sessions_attended=attended, total_sessions=total, status=status,
                       valid_from=datetime.now(timezone.utc)))
    await db_session.flush()
    monkeypatch.setattr('app.modules.students.router.resolve_staff_studio', AsyncMock(return_value=(orgs[0], None)))
    statements = []
    def record(*args): statements.append(args[2])
    engine = db_session.bind.sync_engine
    event.listen(engine, 'before_cursor_execute', record)
    try:
        result = await list_students(page=1, page_size=3, current_user=users[0], db=db_session)
        assert len(statements) == 4  # completion scan, count, page, enrollment aggregates
        assert [(i['plan'], i['attended'], i['total']) for i in result['items']] == [
            ('Enrolled', 5, 5), ('No enrollment', 0, 0), ('Enrolled', 0, 0)]
        statements.clear()
        empty = await list_students(page=3, page_size=3, current_user=users[0], db=db_session)
        assert empty['items'] == [] and empty['total_items'] == 3
        assert len(statements) == 3
    finally:
        event.remove(engine, 'before_cursor_execute', record)
