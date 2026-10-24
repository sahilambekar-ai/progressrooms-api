"""Create isolated synthetic Zoom demo data. Never reads/writes the studio database."""
import asyncio
from pathlib import Path
from cryptography.fernet import Fernet
from dotenv import load_dotenv

ENV_FILE = Path('/private/tmp/progressrooms-zoom-demo.env')
if not ENV_FILE.exists():
    ENV_FILE.write_text('\n'.join([
        'APP_ENV=development', 'ZOOM_PROVIDER_MODE=mock', 'ZOOM_TOKEN_ENCRYPTION_KEY=' + Fernet.generate_key().decode(),
        'ZOOM_CLIENT_ID=mock-platform-client', 'ZOOM_CLIENT_SECRET=mock-platform-secret', 'ZOOM_WEBHOOK_SECRET=mock-webhook-secret',
        'DATABASE_URL=sqlite+aiosqlite:////private/tmp/progressrooms-zoom-demo.db', 'SYNC_DATABASE_URL=sqlite:////private/tmp/progressrooms-zoom-demo.db',
        'ZOOM_REDIRECT_URI=http://localhost:3005/integrations/zoom/callback', 'WEBSITE_PUBLIC_ORIGIN=http://localhost:3005',
        'BACKEND_CORS_ORIGINS=["http://localhost:3005"]', 'BREVO_ENABLED=false',
        'SECRET_KEY=isolated-zoom-demo-authentication-only-not-for-deployment',
    ]) + '\n')
    ENV_FILE.chmod(0o600)
load_dotenv(ENV_FILE, override=True)

from datetime import timedelta
from sqlalchemy import select
from app.core.database import Base, engine, AsyncSessionLocal
from app.core.security import get_password_hash
from app.models.user import User
from app.models.organization import Organization, OrganizationMember, OrganizationSetting, OrganizationStudent
from app.models.session import Session, SessionInstructor, SessionPricing, ClassType
from app.models.schedule import ClassOccurrence
from app.models.commerce import SessionEnrollment
from app.modules.zoom.service import create_attempt, callback, confirm, now
from app.modules.zoom.router import save_defaults, DefaultsRequest
import hashlib

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        if await db.scalar(select(User.id).where(User.email == 'owner@zoom-demo.example.com')):
            print('Isolated Zoom demo already exists. Credentials: owner@zoom-demo.example.com / ZoomDemoOnly2026!')
            return
        org = Organization(name='Zoom Demo Studio', slug='zoom-demo')
        db.add(org); await db.flush()
        users, members = [], []
        for i, name in enumerate(['Demo Owner', 'Asha · Personal Zoom', 'Ravi · Studio Fallback', 'Mira · Reconnect Example']):
            user = User(full_name=name, email=('owner' if i == 0 else f'teacher{i}')+'@zoom-demo.example.com', username=f'zoom_demo_{i}',
                role='STUDIO' if i == 0 else 'TEACHER', password_hash=get_password_hash('ZoomDemoOnly2026!'), account_status='ACTIVE', is_verified=True,
                phone='+91 90000 0000'+str(i), address='Demo teaching campus', location='Pune')
            db.add(user); await db.flush()
            member = OrganizationMember(organization_id=org.id, user_id=user.id, role=user.role, can_teach=True,
                title='Yoga guide', bio='Synthetic development profile for Zoom integration testing.', teacher_code=str(90000001+i))
            db.add(member); users.append(user); members.append(member)
        prefs = OrganizationSetting(organization_id=org.id, account_completed=True, completion_step=6, completion_percentage=100, zoom_auto_meeting_enabled=False)
        db.add(prefs); await db.commit()
        for user, member in zip(users[:2], members[:2]):
            state, _ = await create_attempt(db, member, user, 'OAUTH')
            candidate = await callback(db, state, 'mock_'+hashlib.sha256(state.encode()).hexdigest(), user)
            import uuid
            await confirm(db, uuid.UUID(candidate['confirmation_id']), user)
        await save_defaults(db, org.id, DefaultsRequest(version=prefs.zoom_settings_version, default_host_member_id=members[0].id,
            auto_meeting_enabled=True, acknowledge_impact=True))
        ct = ClassType(organization_id=org.id, name='Demo Yoga', slug='demo-yoga')
        db.add(ct); await db.flush()
        student = User(full_name='Demo Student', email='student@zoom-demo.example.com', username='zoom_demo_student', role='STUDENT',
            password_hash=get_password_hash('ZoomDemoOnly2026!'), account_status='ACTIVE', is_verified=True, phone='+91 90000 00009', location='Pune')
        db.add(student); await db.flush()
        db.add(OrganizationStudent(organization_id=org.id, user_id=student.id, teacher_member_id=members[2].id))
        for i in [1, 2]:
            sess = Session(organization_id=org.id, class_type_id=ct.id, name='Personal host demo' if i == 1 else 'Studio fallback demo',
                slug=f'zoom-demo-{i}', capacity=10, is_public=True)
            db.add(sess); await db.flush()
            db.add(SessionInstructor(session_id=sess.id, member_id=members[i].id, role='PRIMARY'))
            db.add(SessionPricing(session_id=sess.id, price=100, currency='INR', pricing_type='PER_SESSION'))
            start = now() + timedelta(minutes=5)
            db.add(ClassOccurrence(organization_id=org.id, session_id=sess.id, instructor_id=members[i].id, scheduled_start_at=start,
                scheduled_end_at=start+timedelta(minutes=45), actual_start_at=start, actual_end_at=start+timedelta(minutes=45), status='SCHEDULED'))
            db.add(SessionEnrollment(organization_id=org.id, session_id=sess.id, user_id=student.id, status='ACTIVE', valid_from=now()-timedelta(days=1), valid_until=now()+timedelta(days=30), total_sessions=10))
        await db.commit()
    print('Created isolated mock demo. Owner: owner@zoom-demo.example.com; teachers: teacher1/teacher2@zoom-demo.example.com; student: student@zoom-demo.example.com. Demo password: ZoomDemoOnly2026!')
    print('Environment file: /private/tmp/progressrooms-zoom-demo.env (synthetic only). No existing studio database changed.')
    await engine.dispose()

if __name__ == '__main__':
    asyncio.run(main())
