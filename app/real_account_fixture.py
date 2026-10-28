"""Explicit local/UAT fixtures for two real, authorized test recipients. No provider calls."""
import argparse
import asyncio
import json
import os
import secrets
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, EmailStr
from sqlalchemy import select, text, func

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.security import get_password_hash, verify_password
from app.models.attendance import AttendanceRecord, TeacherAttendance
from app.models.commerce import SessionEnrollment
from app.models.communication import UserCommunicationPreference
from app.models.organization import Organization, OrganizationMember, OrganizationSetting, OrganizationStudent
from app.models.plan import OrganizationSubscription, Plan
from app.models.schedule import ClassOccurrence
from app.models.session import ClassType, Session, SessionInstructor, SessionPricing
from app.models.user import User
from app.modules.teachers.codes import generate_teacher_code

NAMESPACE = UUID('56120841-4145-46df-a5eb-b5f9ac89c2ca')


def validate_target(environment, database_url, app_environment):
    if environment == 'uat':
        if app_environment != 'uat':
            raise ValueError('UAT fixture requires APP_ENV=uat')
    elif environment == 'local':
        parsed = urlsplit(database_url)
        if app_environment not in {'development', 'test'} or (not parsed.scheme.startswith('sqlite') and parsed.hostname not in {'localhost', '127.0.0.1', '::1'}):
            raise ValueError('Local fixture requires a local database and development/test environment')
    else:
        raise ValueError('Only local and UAT fixtures are supported')


async def seed(db, environment, teacher_email, student_email, *, known_credentials=None, now=None):
    if environment not in {'local', 'uat'}:
        raise ValueError('Production fixtures are not supported')
    adapter = TypeAdapter(EmailStr)
    teacher_email, student_email = [str(adapter.validate_python(email)).lower() for email in (teacher_email, student_email)]
    if teacher_email == student_email:
        raise ValueError('Teacher and student need separate email addresses')
    now = now or datetime.now(timezone.utc)
    if db.bind.dialect.name == 'postgresql':
        await db.execute(text('SELECT pg_advisory_xact_lock(873125441)'))
    credentials = dict(known_credentials or {})
    users = []
    for email, role, name in [(teacher_email, 'STUDIO', 'Solo Studio Teacher'), (student_email, 'STUDENT', 'Test Student')]:
        user = await db.scalar(select(User).where(User.email == email).with_for_update())
        if user and (user.role != role or user.deleted_at or not user.is_active or user.account_status in {'SUSPENDED', 'DISABLED'}):
            raise ValueError(f'Existing {role.lower()} account has a conflicting role or disabled status; no changes committed')
        if user is None:
            password = secrets.token_urlsafe(24) + '9aA!'
            user = User(email=email, role=role, full_name=name, password_hash=get_password_hash(password),
                        is_active=True, is_verified=True, account_status='ACTIVE', billing_country='IN')
            db.add(user)
            await db.flush()
            credentials[email] = password
        else:
            user.is_verified, user.account_status = True, 'ACTIVE'
            if not user.password_hash:
                password = secrets.token_urlsafe(24) + '9aA!'
                user.password_hash = get_password_hash(password)
                credentials[email] = password
            elif credentials.get(email) and not verify_password(credentials[email], user.password_hash):
                credentials.pop(email)
        pref = await db.scalar(select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == user.id))
        if pref is None:
            db.add(UserCommunicationPreference(user_id=user.id, email_opted_out=False, whatsapp_opted_out=True))
        else:
            pref.email_opted_out = False
        users.append(user)
    teacher, student = users
    members = (await db.scalars(select(OrganizationMember).where(OrganizationMember.user_id == teacher.id,
        OrganizationMember.role == 'STUDIO', OrganizationMember.is_active.is_(True)))).all()
    if len(members) > 1:
        raise ValueError('Teacher owns multiple studios; select the studio explicitly before seeding')
    if members:
        member = members[0]
        org = await db.get(Organization, member.organization_id)
        if not org or org.deleted_at or org.status != 'ACTIVE':
            raise ValueError('Existing studio is not active; no changes committed')
    else:
        slug = f'solo-test-{environment}-{teacher.id.hex[:8]}'
        org = Organization(name=f'Solo Teacher Test · {environment.upper()}', slug=slug, timezone='Asia/Kolkata', currency='INR', status='ACTIVE')
        db.add(org)
        await db.flush()
        member = OrganizationMember(organization_id=org.id, user_id=teacher.id, role='STUDIO', is_active=True,
                                    can_teach=True, teacher_code=await generate_teacher_code(db))
        db.add(member)
        await db.flush()
    member.can_teach = True
    prefs = await db.scalar(select(OrganizationSetting).where(OrganizationSetting.organization_id == org.id))
    if prefs is None:
        db.add(OrganizationSetting(organization_id=org.id, country='India', teaching_mode='ONLINE',
            account_completed=True, completion_step=6, completion_percentage=100, zoom_auto_meeting_enabled=False))
    subscription = await db.scalar(select(OrganizationSubscription).where(OrganizationSubscription.organization_id == org.id))
    if subscription is None:
        plan = await db.scalar(select(Plan).where(Plan.slug == 'core'))
        if not plan:
            raise ValueError('Apply migrations before seeding: Core plan is missing')
        db.add(OrganizationSubscription(organization_id=org.id, plan_id=plan.id, status='ACTIVE', starts_at=now, saas_managed=False))
    roster = await db.scalar(select(OrganizationStudent).where(OrganizationStudent.organization_id == org.id, OrganizationStudent.user_id == student.id))
    if roster is None:
        db.add(OrganizationStudent(organization_id=org.id, user_id=student.id, teacher_member_id=member.id, status='ACTIVE'))
    else:
        roster.status, roster.teacher_member_id = 'ACTIVE', member.id
    def identity(key):
        return uuid5(NAMESPACE, f'{environment}:{org.id}:{student.id}:{key}')
    async def add(model, key, **values):
        row = await db.get(model, identity(key))
        if row is None:
            row = model(id=identity(key), **values)
            db.add(row)
            await db.flush()
        return row
    kind = await add(ClassType, 'class-type', organization_id=org.id, name='Solo test lessons', slug='solo-test-lessons')
    zone = ZoneInfo(org.timezone or 'Asia/Kolkata')
    local = now.astimezone(zone)
    today = local.date()
    first_hour = min(local.hour + 1, 21)
    starts = [(-4, 9, 'ATTENDED'), (-2, 9, 'ABSENT'), (0, first_hour, None), (1, 10, None)]
    course = await add(Session, 'course', organization_id=org.id, class_type_id=kind.id,
        name='Sample course · Solo teacher', slug='solo-test-course', session_type='GROUP', capacity=10,
        duration_minutes=45, is_active=True, is_public=False, full_description='Complimentary test course with past, today and tomorrow classes.')
    await add(SessionInstructor, 'course-teacher', session_id=course.id, member_id=member.id, role='PRIMARY')
    await add(SessionPricing, 'course-pricing', session_id=course.id, pricing_type='PACKAGE', price=0, currency='INR',
        total_classes=7, validity_days=60, packages=[{'id':str(identity('course-package')), 'total_classes':7, 'price':0, 'offer_price':None, 'validity_days':60}])
    enrollment = await add(SessionEnrollment, 'course-enrollment', organization_id=org.id, session_id=course.id,
        user_id=student.id, status='ACTIVE', valid_from=now-timedelta(days=30), valid_until=now+timedelta(days=30),
        total_sessions=7, sessions_attended=0, billing_value=0, billing_currency='INR', billing_type='PACKAGE', billing_label='Complimentary sample course')
    occurrences = []
    for offset, hour, attendance in starts:
        day = today + timedelta(days=offset)
        start = datetime.combine(day, time(hour), zone).astimezone(timezone.utc)
        end = start + timedelta(minutes=45)
        occurrence = await add(ClassOccurrence, f'course-occurrence:{day}', organization_id=org.id, session_id=course.id,
            instructor_id=member.id, scheduled_start_at=start, scheduled_end_at=end, actual_start_at=start, actual_end_at=end,
            status='COMPLETED' if end <= now else 'SCHEDULED')
        if attendance:
            await add(AttendanceRecord, f'course-attendance:{day}', class_occurrence_id=occurrence.id,
                session_enrollment_id=enrollment.id, status=attendance, marked_by_user_id=teacher.id, marked_at=end, notes='Seeded test attendance')
            await add(TeacherAttendance, f'course-teacher-attendance:{day}', class_occurrence_id=occurrence.id,
                teacher_member_id=member.id, status='PRESENT', marked_by_user_id=teacher.id, notes='Seeded test attendance')
        occurrences.append(occurrence)
    for offset, hour in [(-1, 15), (0, min(first_hour+2, 23)), (1, 15)]:
        day = today + timedelta(days=offset)
        start = datetime.combine(day, time(hour), zone).astimezone(timezone.utc)
        end = start + timedelta(minutes=45)
        ended = end <= now
        appointment = await add(Session, f'appointment:{day}', organization_id=org.id, class_type_id=kind.id,
            name=f'Private lesson · {day}', slug=f'solo-private-{day}', session_type='ONE_TO_ONE', capacity=1,
            duration_minutes=45, is_active=not ended, is_public=False, deleted_at=now if ended else None,
            full_description='Free one-on-one appointment assigned to the test student.')
        await add(SessionInstructor, f'appointment-teacher:{day}', session_id=appointment.id, member_id=member.id, role='PRIMARY')
        await add(SessionPricing, f'appointment-pricing:{day}', session_id=appointment.id, pricing_type='PER_SESSION',
                  price=0, currency='INR', total_classes=1, validity_days=7)
        own_enrollment = await add(SessionEnrollment, f'appointment-enrollment:{day}', organization_id=org.id,
            session_id=appointment.id, user_id=student.id, status='ACTIVE', valid_from=start, valid_until=end+timedelta(days=1),
            total_sessions=1, sessions_attended=1 if ended else 0, billing_value=0, billing_currency='INR',
            billing_type='PER_SESSION', billing_label=appointment.name)
        occurrence = await add(ClassOccurrence, f'appointment-occurrence:{day}', organization_id=org.id,
            session_id=appointment.id, instructor_id=member.id, scheduled_start_at=start, scheduled_end_at=end,
            actual_start_at=start, actual_end_at=end, status='COMPLETED' if ended else 'SCHEDULED')
        if ended:
            await add(AttendanceRecord, f'appointment-attendance:{day}', class_occurrence_id=occurrence.id,
                session_enrollment_id=own_enrollment.id, status='ATTENDED', marked_by_user_id=teacher.id, marked_at=end, notes='Seeded past private lesson')
            await add(TeacherAttendance, f'appointment-teacher-attendance:{day}', class_occurrence_id=occurrence.id,
                teacher_member_id=member.id, status='PRESENT', marked_by_user_id=teacher.id, notes='Seeded past private lesson')
        occurrences.append(occurrence)
    await db.flush()
    enrollment.sessions_attended = await db.scalar(select(func.count()).select_from(AttendanceRecord).where(
        AttendanceRecord.session_enrollment_id == enrollment.id, AttendanceRecord.status == 'ATTENDED'))
    return {'environment':environment, 'studio_id':str(org.id), 'studio_slug':org.slug, 'teacher_id':str(teacher.id),
            'teacher_member_id':str(member.id), 'student_id':str(student.id), 'timezone':str(zone), 'today':str(today),
            'occurrence_ids':[str(o.id) for o in occurrences], 'credentials':credentials}


def write_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(data, handle, indent=2)
    path.chmod(0o600)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', required=True, choices=['local', 'uat'])
    parser.add_argument('--teacher-email', required=True)
    parser.add_argument('--student-email', required=True)
    parser.add_argument('--credentials-path', required=True)
    args = parser.parse_args()
    validate_target(args.environment, settings.DATABASE_URL, settings.APP_ENV)
    path = Path(args.credentials_path)
    existing = json.loads(path.read_text()).get('credentials', {}) if path.exists() else {}
    try:
        async with AsyncSessionLocal() as db:
            result = await seed(db, args.environment, args.teacher_email, args.student_email, known_credentials=existing)
            # Write the recovery credentials before commit so an I/O failure cannot strand a new account.
            write_private(path, result)
            await db.commit()
            print(json.dumps({key:value for key,value in result.items() if key != 'credentials'}))
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
