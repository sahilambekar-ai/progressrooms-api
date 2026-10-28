"""Regression coverage for scheduling, assignment, email and deletion as one flow."""
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from app.core.database import get_db
from app.main import app, api_v1
from app.models.user import User
from app.models.session import Session
from app.models.schedule import ClassOccurrence
from app.models.commerce import SessionEnrollment
from app.models.communication import MeetingEmailDelivery, CommunicationLog, ClassReminderSchedule, UserCommunicationPreference
from app.modules.communication.worker import deliver_one
from test_teacher_zoom import studio, zoom_config, authorize, enable
from test_storefront import auth


@pytest_asyncio.fixture
async def appointments(db_session, studio):
    org, owner, _, member, _ = studio
    students = [User(email=f'{uuid.uuid4()}@example.test', full_name=f'Student {i}', role='STUDENT',
                     is_verified=True, account_status='ACTIVE') for i in range(2)]
    db_session.add_all(students)
    await db_session.commit()
    await authorize(db_session, org, member, owner)
    await enable(db_session, org, member)
    async def dependency():
        yield db_session
    api_v1.dependency_overrides[get_db] = dependency
    tomorrow = datetime.now(ZoneInfo(org.timezone)).date() + timedelta(days=1)
    payload = dict(title='Private appointment', teacher_member_id=str(member.id), student_user_id=str(students[0].id),
                   date=tomorrow.isoformat(), start_time='10:00', end_time='11:00', price=0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test/api/v1') as client:
        yield client, org, owner, member, students, payload
    api_v1.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_private_appointment_create_email_join_edit_delete(appointments, db_session):
    client, org, owner, teacher, students, payload = appointments
    base = f'/organizations/{org.id}'
    created = await client.post(base+'/one-on-one-classes', headers=auth(owner), json=payload)
    assert created.status_code == 201, created.text
    value = created.json()
    class_id, occurrence_id = value['id'], value['occurrence_id']
    session = await db_session.get(Session, uuid.UUID(class_id))
    occurrence = await db_session.get(ClassOccurrence, uuid.UUID(occurrence_id))
    deliveries = list((await db_session.scalars(select(MeetingEmailDelivery).where(
        MeetingEmailDelivery.class_occurrence_id == occurrence.id))).all())
    assert {d.recipient_user_id for d in deliveries} == {owner.id, students[0].id}
    assert all(d.message_type == 'CLASS_INVITE' for d in deliveries)
    assert await db_session.scalar(select(ClassReminderSchedule.id).where(ClassReminderSchedule.class_occurrence_id == occurrence.id))
    for delivery in deliveries:
        await deliver_one(db_session, delivery.id)
        await db_session.refresh(delivery)
        assert delivery.status == 'SIMULATED'
    assert len((await db_session.scalars(select(CommunicationLog).where(CommunicationLog.organization_id == org.id))).all()) == 2
    # Students see and join their assigned class even when the catalog Live flag is off,
    # before the enrollment start, and after attendance has been marked.
    session.is_active = False
    enrollment = await db_session.scalar(select(SessionEnrollment).where(SessionEnrollment.session_id == session.id))
    enrollment.sessions_attended = 1
    await db_session.commit()
    upcoming = await client.get('/student/upcoming-classes', headers=auth(students[0]))
    assert occurrence_id in {c['id'] for c in upcoming.json()}, upcoming.text
    assert (await client.get('/student/upcoming-classes', headers=auth(students[1]))).json() == []
    own_join = await client.get(f'/student/classes/{occurrence_id}/meeting-url', headers=auth(students[0]))
    assert own_join.status_code == 200, own_join.text
    assert set(own_join.json()) == {'meeting_url', 'provider', 'is_simulated'}
    assert (await client.get(f'/student/classes/{occurrence_id}/meeting-url', headers=auth(students[1]))).status_code == 403
    # Public visibility cannot expose a private appointment, even for legacy data.
    session.is_public, session.is_active = True, True
    await db_session.commit()
    catalog = (await client.get(f'/public/organizations/{org.slug}')).json()
    assert class_id not in {s['id'] for s in catalog['sessions']}
    assert (await client.get(f'/public/organizations/{org.slug}/classes/{session.slug}')).status_code == 404
    assert (await client.post('/commerce/orders', headers=auth(students[1]), json={'session_id':class_id})).status_code == 400
    assert (await client.put(base+f'/classes/{class_id}', headers=auth(owner), json={'name':'Wrong editor'})).status_code == 409
    # The appointment editor changes the actual occurrence and assignment together.
    edited = await client.put(base+f'/one-on-one-classes/{class_id}', headers=auth(owner),
        json={**payload, 'title':'Updated appointment', 'student_user_id':str(students[1].id), 'start_time':'12:00', 'end_time':'13:30'})
    assert edited.status_code == 200, edited.text
    unassigned_notice = await db_session.scalar(select(MeetingEmailDelivery).where(
        MeetingEmailDelivery.class_occurrence_id == occurrence.id, MeetingEmailDelivery.event_key.like('unassigned:%')))
    assert unassigned_notice.recipient_user_id == students[0].id
    await deliver_one(db_session, unassigned_notice.id)
    await db_session.refresh(unassigned_notice)
    assert unassigned_notice.status == 'SIMULATED'
    await db_session.refresh(occurrence)
    assert occurrence.actual_start_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(org.timezone)).hour == 12
    assert session.duration_minutes == 90 and not session.is_public
    listed = (await client.get(base+'/classes', headers=auth(owner))).json()
    assert next(s for s in listed if s['id'] == class_id)['assigned_student_user_id'] == str(students[1].id)
    assert (await client.get('/student/upcoming-classes', headers=auth(students[0]))).json() == []
    assert (await client.get('/student/upcoming-classes', headers=auth(students[1]))).json()[0]['name'] == 'Updated appointment'
    month = payload['date'][:7]
    assert (await client.get(f'/student/attendance?month={month}', headers=auth(students[0]))).json() == []
    agenda = (await client.get(base+'/occurrences/teacher-agenda', headers=auth(owner))).json()
    assert occurrence_id in {c['id'] for c in agenda['classes']}
    assert next(c for c in agenda['classes'] if c['id'] == occurrence_id)['email_deliveries']
    deleted = await client.delete(base+f'/classes/{class_id}', headers=auth(owner))
    assert deleted.status_code == 204, deleted.text
    assert (await client.get(base+'/occurrences/teacher-agenda', headers=auth(owner))).json()['classes'] == []
    assert (await client.get('/student/upcoming-classes', headers=auth(students[1]))).json() == []
    assert (await client.get(f'/student/attendance?month={month}', headers=auth(students[1]))).json() == []
    assert (await client.get(base+f'/attendance/teachers/{teacher.id}?month={month}', headers=auth(owner))).json() == []
    assert (await client.get(base+f'/attendance/students/{students[1].id}?month={month}', headers=auth(owner))).json() == []
    assert (await client.get(f'/student/classes/{occurrence_id}/meeting-url', headers=auth(students[1]))).status_code == 404
    cancellations = (await db_session.scalars(select(MeetingEmailDelivery).where(
        MeetingEmailDelivery.class_occurrence_id == occurrence.id, MeetingEmailDelivery.event_key == f'cancel:{occurrence.id}'))).all()
    assert {row.recipient_user_id for row in cancellations} == {owner.id, students[1].id}


@pytest.mark.asyncio
async def test_deleting_started_appointment_cancels_and_removes_it(appointments, db_session):
    client, org, owner, teacher, students, payload = appointments
    created = (await client.post(f'/organizations/{org.id}/one-on-one-classes', headers=auth(owner), json=payload)).json()
    occurrence = await db_session.get(ClassOccurrence, uuid.UUID(created['occurrence_id']))
    now = datetime.now(timezone.utc)
    occurrence.actual_start_at, occurrence.actual_end_at, occurrence.status = now-timedelta(minutes=10), now+timedelta(minutes=50), 'IN_PROGRESS'
    await db_session.commit()
    deleted = await client.delete(f'/organizations/{org.id}/classes/{created["id"]}', headers=auth(owner))
    assert deleted.status_code == 204, deleted.text
    await db_session.refresh(occurrence)
    assert occurrence.status == 'CANCELLED'
    agenda = await client.get(f'/organizations/{org.id}/occurrences/teacher-agenda', headers=auth(owner))
    assert agenda.json()['classes'] == []


@pytest.mark.asyncio
async def test_seeded_recipient_optout_is_reported_before_queueing(appointments, db_session):
    client, org, owner, teacher, students, payload = appointments
    created = (await client.post(f'/organizations/{org.id}/one-on-one-classes', headers=auth(owner), json=payload)).json()
    for user in [owner, students[0]]:
        db_session.add(UserCommunicationPreference(user_id=user.id, email_opted_out=True))
    await db_session.commit()
    response = await client.post(f'/organizations/{org.id}/occurrences/{created["occurrence_id"]}/send-invite', headers=auth(owner))
    assert response.status_code == 409 and 'No recipients have class email enabled' in response.json()['detail']
    delivery = await db_session.scalar(select(MeetingEmailDelivery).where(MeetingEmailDelivery.recipient_user_id == students[0].id))
    await deliver_one(db_session, delivery.id)
    log = await db_session.scalar(select(CommunicationLog).where(CommunicationLog.recipient_user_id == students[0].id))
    assert log.status == 'SUPPRESSED' and log.suppression_reason == 'Recipient has opted out of email notifications'


@pytest.mark.asyncio
async def test_student_is_never_told_to_authorize_the_host_account(appointments, db_session):
    client, org, owner, teacher, students, payload = appointments
    created = (await client.post(f'/organizations/{org.id}/one-on-one-classes', headers=auth(owner), json=payload)).json()
    from app.models.zoom import TeacherZoomConnection
    connection = await db_session.scalar(select(TeacherZoomConnection).where(TeacherZoomConnection.member_id == teacher.id))
    connection.status = 'RECONNECT_REQUIRED'
    await db_session.commit()
    response = await client.get(f'/student/classes/{created["occurrence_id"]}/meeting-url', headers=auth(students[0]))
    assert response.status_code == 409
    assert 'students do not need to connect Zoom' in response.json()['detail']
