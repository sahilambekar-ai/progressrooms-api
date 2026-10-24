"""Repeatable, explicitly enabled validation tenants for UAT and production."""
import argparse
import asyncio
import hashlib
import hmac
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone, time
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo
from sqlalchemy import select, text
from app.core.database import AsyncSessionLocal
from app.core.security import get_password_hash
from app.models.user import User
from app.models.organization import Organization, OrganizationMember, OrganizationSetting, OrganizationStudent
from app.models.plan import Plan, OrganizationSubscription
from app.models.session import ClassType, Session, SessionInstructor, SessionPricing
from app.models.schedule import ClassOccurrence, SessionScheduleRule
from app.models.commerce import SessionEnrollment
from app.models.attendance import AttendanceRecord, TeacherAttendance
from app.models.communication import UserCommunicationPreference
from app.modules.teachers.codes import generate_teacher_code

NAMESPACE = UUID('e8325f66-d597-4dc9-8823-4be5636a1a41')
TENANTS = [('solo-one', 'Validation Solo Teacher One', 0, 10),
           ('solo-two', 'Validation Solo Teacher Two', 0, 10),
           ('lotus', 'Validation Lotus Studio', 5, 20),
           ('rhythm', 'Validation Rhythm Academy', 4, 15)]


def identity(profile, key):
    return uuid5(NAMESPACE, f'{profile}:{key}')


def password(secret, profile, key):
    return 'Vr!' + hmac.new(secret.encode(), f'v1:{profile}:{key}'.encode(), hashlib.sha256).hexdigest()[:22] + '9aA'


def accounts(profile):
    for slug, name, teachers, students in TENANTS:
        for role, count in [('owner', 1), ('teacher', teachers), ('student', students)]:
            for index in range(1, count + 1):
                key = f'{slug}.{role}{index:02}'
                yield slug, role, index, key, f'validation.{profile}.{key}@example.com'


def credential_document(profile, secret):
    lines = [f'# Progress Rooms — {profile.upper()} validation credentials', '',
             'Private: do not commit or share publicly. These initial passwords are created when the seed runs.',
             'Use your deployment website /login. Accounts are pre-verified; no email/OTP delivery is required for password login.',
             'Existing accounts and passwords are never reset. A password changed in the application supersedes this document.',
             'These are synthetic validation accounts, with complimentary plan access and no payment transactions or Zoom authorization.', '']
    for slug, name, teachers, students in TENANTS:
        lines += [f'## {name}', '', f'Studio slug: `validation-{profile}-{slug}`. {teachers} additional teachers; {students} students.', '']
        for role in ('owner', 'teacher', 'student'):
            rows = [row for row in accounts(profile) if row[0] == slug and row[1] == role]
            if not rows:
                continue
            lines += [f'### {"Studio owner / standalone teacher" if role == "owner" else role.title() + "s"}', '', '| Account | Email / login | Initial password |', '| --- | --- | --- |']
            for _, _, index, key, email in rows:
                lines.append(f'| {role.title()} {index:02} | {email} | `{password(secret, profile, key)}` |')
            lines.append('')
    lines += ['## Validation checklist', '',
              '- Owners: teachers, students, courses, upcoming classes, attendance and billing menus.',
              '- Teachers: assigned courses and attendance; owner-only actions remain unavailable.',
              '- Students: enrolled courses, upcoming classes and mixed attendance history.',
              '- The final two students per studio start with expired and paused enrollments, respectively.',
              '- History includes attended, absent and excused marks; dates extend through the next 28 days on every seed run.',
              '- Courses are private. No real payment, outbound notification, Zoom meeting, bank details or superadmin account is seeded.', '']
    return '\n'.join(lines)


def write_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(content)
    path.chmod(0o600)


async def seed(db, profile, secret, now=None):
    if profile not in {'uat', 'prod'} or len(secret) < 32:
        raise ValueError('Use profile uat/prod and a validation seed secret of at least 32 characters')
    now = now or datetime.now(timezone.utc)
    if db.bind.dialect.name == 'postgresql':
        await db.execute(text('SELECT pg_advisory_xact_lock(873125440)'))
    async def add(model, key, **values):
        pk = identity(profile, key)
        row = await db.get(model, pk)
        if row is None:
            row = model(id=pk, **values)
            db.add(row)
            await db.flush()
        return row
    total = {'studios': 0, 'owners': 0, 'teachers': 0, 'students': 0}
    for slug, name, teacher_count, student_count in TENANTS:
        org_slug = f'validation-{profile}-{slug}'
        existing = await db.scalar(select(Organization).where(Organization.slug == org_slug))
        if existing and existing.id != identity(profile, slug):
            raise ValueError(f'Validation slug collision: {org_slug}; no existing tenant will be modified')
        org = await add(Organization, slug, name=f'[{profile.upper()} TEST] {name}', slug=org_slug, timezone='Asia/Kolkata')
        total['studios'] += 1
        plan = await db.scalar(select(Plan).where(Plan.slug == ('studio' if teacher_count else 'core')))
        if not plan:
            raise ValueError('Plan catalog missing: apply database migrations before seeding')
        await add(OrganizationSubscription, f'{slug}:plan', organization_id=org.id, plan_id=plan.id,
                  status='ACTIVE', starts_at=now, saas_managed=False)
        await add(OrganizationSetting, f'{slug}:settings', organization_id=org.id, country='India', city='Pune',
                  teaching_mode='ONLINE', account_completed=True, completion_step=6, completion_percentage=100,
                  zoom_auto_meeting_enabled=False)
        members, students = [], []
        for tenant, role, index, key, email in accounts(profile):
            if tenant != slug:
                continue
            collision = await db.scalar(select(User).where(User.email == email))
            if collision and collision.id != identity(profile, key):
                raise ValueError(f'Validation account collision: {email}; no existing user will be modified')
            user = await db.get(User, identity(profile, key))
            if user is None:
                user = await add(User, key, email=email, full_name=f'{name} {role.title()} {index:02}',
                    role='STUDIO' if role == 'owner' else role.upper(), is_verified=True, account_status='ACTIVE',
                    billing_country='IN', password_hash=get_password_hash(password(secret, profile, key)))
            await add(UserCommunicationPreference, f'{key}:communications', user_id=user.id,
                      email_opted_out=True, whatsapp_opted_out=True)
            total[{'owner': 'owners', 'teacher': 'teachers', 'student': 'students'}[role]] += 1
            if role != 'student':
                member = await db.get(OrganizationMember, identity(profile, key + ':member'))
                if member is None:
                    member = await add(OrganizationMember, key + ':member', organization_id=org.id, user_id=user.id,
                        role=user.role, can_teach=True, title='Validation instructor', teacher_code=await generate_teacher_code(db))
                members.append(member)
            else:
                students.append((key, user))
        for index, (key, user) in enumerate(students):
            await add(OrganizationStudent, key + ':roster', organization_id=org.id, user_id=user.id,
                      teacher_member_id=members[index % len(members)].id, student_notes='Synthetic validation record')
        for index, member in enumerate(members):
            course_key = f'{slug}:course:{index}'
            category = await add(ClassType, course_key + ':type', organization_id=org.id,
                                 name='Validation course', slug=f'validation-course-{index}')
            course = await add(Session, course_key, organization_id=org.id, class_type_id=category.id,
                name=f'Validation {"Yoga Foundations" if index % 2 == 0 else "Movement Course"} {index + 1}',
                slug=f'validation-course-{index + 1}', capacity=30, duration_minutes=60, is_public=False,
                full_description='Synthetic validation course. No real payment or Zoom meeting is configured.')
            await add(SessionInstructor, course_key + ':instructor', session_id=course.id, member_id=member.id)
            await add(SessionPricing, course_key + ':price', session_id=course.id, pricing_type='MONTHLY', price=500, validity_days=30)
            for day in (0, 2, 4):
                await add(SessionScheduleRule, f'{course_key}:rule:{day}', session_id=course.id, day_of_week=day,
                          start_time=time(7 + index, 0), end_time=time(8 + index, 0), effective_start_date=(now - timedelta(days=14)).date())
            enrollments = []
            for student_index, (key, user) in enumerate(students):
                if student_index % len(members) != index:
                    continue
                status = 'EXPIRED' if student_index == len(students) - 1 else 'PAUSED' if student_index == len(students) - 2 else 'ACTIVE'
                enrollment = await add(SessionEnrollment, f'{course_key}:{key}:enrollment', organization_id=org.id,
                    session_id=course.id, user_id=user.id, status=status, valid_from=now - timedelta(days=30),
                    valid_until=now - timedelta(days=2) if status == 'EXPIRED' else None,
                    paused_at=now - timedelta(days=1) if status == 'PAUSED' else None,
                    billing_value=0, billing_label='Complimentary validation enrollment')
                enrollments.append(enrollment)
            for offset in range(-14, 29):
                day = (now.astimezone(ZoneInfo('Asia/Kolkata')) + timedelta(days=offset)).date()
                if day.weekday() not in (0, 2, 4):
                    continue
                start = datetime.combine(day, time(7 + index), ZoneInfo('Asia/Kolkata')).astimezone(timezone.utc)
                occurrence_key = f'{course_key}:{day.isoformat()}'
                occ = await add(ClassOccurrence, occurrence_key, organization_id=org.id, session_id=course.id,
                    instructor_id=member.id, scheduled_start_at=start, scheduled_end_at=start + timedelta(hours=1),
                    actual_start_at=start, actual_end_at=start + timedelta(hours=1), status='COMPLETED' if start < now else 'SCHEDULED')
                if start >= now:
                    continue
                await add(TeacherAttendance, occurrence_key + ':teacher-attendance', class_occurrence_id=occ.id,
                          teacher_member_id=member.id, status='PRESENT')
                for student_index, enrollment in enumerate(enrollments):
                    mark = ['ATTENDED', 'ABSENT', 'EXCUSED'][(student_index + day.day) % 3]
                    await add(AttendanceRecord, f'{occurrence_key}:{enrollment.id}:attendance', class_occurrence_id=occ.id,
                        session_enrollment_id=enrollment.id, status=mark, marked_by_user_id=member.user_id,
                        marked_at=start + timedelta(hours=1), notes='Synthetic validation attendance')
                # Keep counters aligned without rewriting existing attendance or enrollment status.
            for enrollment in enrollments:
                from sqlalchemy import func
                enrollment.sessions_attended = await db.scalar(select(func.count(AttendanceRecord.id)).where(
                    AttendanceRecord.session_enrollment_id == enrollment.id, AttendanceRecord.status == 'ATTENDED'))
    await db.commit()
    return total


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-only', action='store_true')
    args = parser.parse_args()
    if not args.credentials_only and os.environ.get('VALIDATION_SEED_ENABLED', 'false').lower() != 'true':
        return
    profile = os.environ.get('VALIDATION_SEED_PROFILE', '')
    secret = os.environ.get('VALIDATION_SEED_SECRET', '')
    if profile not in {'uat', 'prod'} or len(secret) < 32:
        raise ValueError('Configure VALIDATION_SEED_PROFILE=uat/prod and VALIDATION_SEED_SECRET (32+ characters)')
    if not args.credentials_only:
        async with AsyncSessionLocal() as db:
            counts = await seed(db, profile, secret)
        print(f'Validation seed complete ({profile}): {counts}; passwords are not logged.')
    write_private(os.environ.get('VALIDATION_CREDENTIALS_PATH', 'data/validation-credentials.md'), credential_document(profile, secret))


if __name__ == '__main__':
    asyncio.run(main())
