import uuid
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from sqlalchemy import select, func
from app.main import app, api_v1
from app.core.database import get_db
from app.core.security import create_access_token
from app.models.user import User
from app.models.organization import Organization, OrganizationMember
from app.models.session import ClassType, Session, SessionInstructor
from app.models.schedule import ClassOccurrence
from app.models.attendance import TeacherAttendance, AttendanceRecord
from app.models.commerce import SessionEnrollment


def headers(user):
    return {'Authorization': f"Bearer {create_access_token({'sub': str(user.id)})}"}


@pytest.mark.asyncio
async def test_teacher_calendar_persistence_permissions_and_local_dates(db_session):
    owner = User(email=f'{uuid.uuid4()}@test.local', full_name='Owner', role='STUDIO', is_verified=True, account_status='ACTIVE')
    teacher = User(email=f'{uuid.uuid4()}@test.local', full_name='Teacher', role='TEACHER', is_verified=True, account_status='ACTIVE')
    student = User(email=f'{uuid.uuid4()}@test.local', full_name='Student', role='STUDENT', is_verified=True, account_status='ACTIVE')
    org = Organization(name='Calendar studio', slug=f'calendar-{uuid.uuid4()}', timezone='Asia/Kolkata', status='ACTIVE')
    db_session.add_all([owner,teacher,student,org]); await db_session.flush()
    owner_member = OrganizationMember(organization_id=org.id,user_id=owner.id,role='STUDIO',is_active=True)
    member = OrganizationMember(organization_id=org.id,user_id=teacher.id,role='TEACHER',is_active=True,teacher_code='12345678')
    ct = ClassType(organization_id=org.id,name='Yoga',slug='yoga')
    db_session.add_all([owner_member,member,ct]); await db_session.flush()
    session = Session(organization_id=org.id,class_type_id=ct.id,name='Midnight yoga',slug='midnight',is_active=True)
    db_session.add(session); await db_session.flush()
    db_session.add(SessionInstructor(session_id=session.id,member_id=member.id,role='PRIMARY'))
    start = datetime(2026,10,14,19,tzinfo=timezone.utc)
    occurrence = ClassOccurrence(organization_id=org.id,session_id=session.id,scheduled_start_at=start,
        scheduled_end_at=start+timedelta(hours=1),actual_start_at=start,actual_end_at=start+timedelta(hours=1),status='COMPLETED')
    enrollment = SessionEnrollment(organization_id=org.id,session_id=session.id,user_id=student.id,status='ACTIVE',
        valid_from=start-timedelta(days=1),valid_until=start+timedelta(days=30),total_sessions=10)
    db_session.add_all([occurrence,enrollment]); await db_session.commit()
    async def dependency(): yield db_session
    api_v1.dependency_overrides[get_db] = dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test/api/v1') as client:
            root=f'/organizations/{org.id}/attendance'
            url=f'{root}/teachers/{member.id}'
            response=await client.get(url+'?month=2026-10',headers=headers(owner))
            assert response.status_code==200,response.text
            assert response.json()[0]['date']=='2026-10-15'
            assert response.json()[0]['status']=='PRESENT'
            assert response.json()[0]['teacherName']=='Teacher'
            target=f'{url}/occurrences/{occurrence.id}'
            assert (await client.put(target,json={'status':'PRESENT'},headers=headers(teacher))).status_code==403
            for value in ['PRESENT','ABSENT','EXCUSED']:
                response=await client.put(target,json={'status':value,'notes':'Owner correction'},headers=headers(owner))
                assert response.status_code==200,response.text
                saved=(await client.get(url+'?month=2026-10',headers=headers(owner))).json()[0]
                assert saved['status']==value and saved['remarks']=='Owner correction'
            assert await db_session.scalar(select(func.count()).select_from(TeacherAttendance))==1
            assert (await client.put(target,json={'status':'INVALID'},headers=headers(owner))).status_code==422
            assert (await client.get(f'{root}/teachers/{owner_member.id}?month=2026-10',headers=headers(teacher))).status_code==403
            assert (await client.get(url+'?month=2026-13',headers=headers(owner))).status_code==422
            assert (await client.get(url+'?month=2026-10',headers=headers(student))).status_code==403
            assert (await client.put(f'{root}/teachers/{owner_member.id}/occurrences/{occurrence.id}',json={'status':'PRESENT'},headers=headers(owner))).status_code==404
            student_url=f'{root}/students/{student.id}?month=2026-10'
            student_self_url='/student/attendance?month=2026-10'
            assert (await client.get(student_url,headers=headers(owner))).json()[0]['status']=='PRESENT'
            assert (await client.get(student_self_url,headers=headers(student))).json()[0]['status']=='PRESENT'
            db_session.add(AttendanceRecord(class_occurrence_id=occurrence.id,session_enrollment_id=enrollment.id,status='ATTENDED'))
            await db_session.commit()
            assert (await client.get(student_url,headers=headers(owner))).json()[0]['status']=='PRESENT'
            self_res = (await client.get(student_self_url,headers=headers(student))).json()
            assert self_res[0]['status']=='PRESENT'
            assert self_res[0]['studentName']=='Student'
            assert (await client.get(url+'?month=2026-11',headers=headers(owner))).json()==[]
    finally:
        api_v1.dependency_overrides.clear()
