from datetime import datetime, timezone
import httpx
import pytest
from sqlalchemy import select, func
from app.main import app, api_v1
from app.core.database import get_db
from app.core.security import verify_password
from app.models.user import User
from app.models.organization import Organization, OrganizationMember
from app.models.session import Session
from app.models.schedule import ClassOccurrence
from app.models.attendance import AttendanceRecord
from app.models.commerce import SessionEnrollment
from app.models.communication import UserCommunicationPreference
from app.validation_seed import seed, identity, password, credential_document


@pytest.mark.asyncio
async def test_seed_is_repeatable_and_accounts_can_login(db_session):
    secret = 'validation-unit-test-secret-only-1234567890'
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    counts = await seed(db_session, 'uat', secret, now)
    assert counts == {'studios': 4, 'owners': 4, 'teachers': 9, 'students': 55}
    seeded_orgs = [identity("uat", key) for key in ("solo-one", "solo-two", "lotus", "rhythm")]
    assert await db_session.scalar(select(func.count(Session.id)).where(Session.organization_id.in_(seeded_orgs))) == 13
    assert await db_session.scalar(select(func.count(ClassOccurrence.id)).where(ClassOccurrence.actual_start_at > now)) > 0
    assert set((await db_session.scalars(select(AttendanceRecord.status).join(SessionEnrollment, SessionEnrollment.id == AttendanceRecord.session_enrollment_id).where(SessionEnrollment.organization_id.in_(seeded_orgs)).distinct())).all()) == {'ATTENDED', 'ABSENT', 'EXCUSED'}
    assert set((await db_session.scalars(select(SessionEnrollment.status).where(SessionEnrollment.organization_id.in_(seeded_orgs)).distinct())).all()) == {'ACTIVE', 'EXPIRED', 'PAUSED'}
    assert not await db_session.scalar(select(func.count(Session.id)).where(Session.is_public.is_(True), Session.organization_id.in_(seeded_orgs)))
    assert await db_session.scalar(select(func.count(UserCommunicationPreference.id)).where(UserCommunicationPreference.email_opted_out.is_(True), UserCommunicationPreference.user_id.in_(select(User.id).where(User.email.like("validation.uat.%@example.com"))))) == 68
    student = await db_session.get(User, identity('uat', 'lotus.student01'))
    assert verify_password(password(secret, 'uat', 'lotus.student01'), student.password_hash)
    student.full_name = 'Validation name edited by tester'
    await db_session.commit()
    snapshot = [await db_session.scalar(select(func.count(model.id))) for model in (User, Organization, OrganizationMember, Session, ClassOccurrence, AttendanceRecord, SessionEnrollment)]
    await seed(db_session, 'uat', secret, now)
    assert snapshot == [await db_session.scalar(select(func.count(model.id))) for model in (User, Organization, OrganizationMember, Session, ClassOccurrence, AttendanceRecord, SessionEnrollment)]
    await db_session.refresh(student)
    assert student.full_name == 'Validation name edited by tester'
    async def override():
        yield db_session
    api_v1.dependency_overrides[get_db] = override
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for key, role in [('lotus.owner01', 'STUDIO'), ('lotus.teacher01', 'TEACHER'), ('lotus.student01', 'STUDENT')]:
                result = await client.post('/api/v1/auth/login', json={'email': f'validation.uat.{key}@example.com', 'password': password(secret, 'uat', key)})
                assert result.status_code == 200, result.text
                assert result.json()['user']['role'] == role
    finally:
        api_v1.dependency_overrides.clear()


def test_credentials_separate_environments_and_roles():
    secret = 'validation-unit-test-secret-only-1234567890'
    assert password(secret, 'uat', 'lotus.owner01') != password(secret, 'prod', 'lotus.owner01')
    doc = credential_document('uat', secret)
    assert doc.count('@example.com') == 68
    assert '### Teachers' in doc and '### Students' in doc
    assert 'Validation Lotus Studio' in doc


@pytest.mark.asyncio
async def test_invalid_seed_configuration_refuses_to_run(db_session):
    with pytest.raises(ValueError):
        await seed(db_session, 'prod', 'short')
