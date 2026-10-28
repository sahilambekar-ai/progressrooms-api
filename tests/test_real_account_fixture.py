from datetime import datetime, timezone
from sqlalchemy import select, func
import pytest
from app.real_account_fixture import seed, validate_target
from app.core.security import verify_password
from app.models.user import User
from app.models.schedule import ClassOccurrence
from app.models.organization import OrganizationSetting
from app.models.communication import UserCommunicationPreference, MeetingEmailDelivery
from app.models.attendance import AttendanceRecord


@pytest.mark.parametrize('environment,url,app_env', [('uat','postgresql://db/test','production'),('local','postgresql://remote/test','development'),('local','sqlite:///test','production'),('production','sqlite:///test','test')])
def test_fixture_rejects_wrong_environment(environment,url,app_env):
    with pytest.raises(ValueError):
        validate_target(environment,url,app_env)


@pytest.mark.asyncio
async def test_fixture_creates_two_verified_accounts_dates_and_preserves_existing_data(db_session):
    now=datetime(2026,10,28,12,tzinfo=timezone.utc)
    result=await seed(db_session,'local','teacher@example.com','student@example.com',now=now)
    await db_session.commit()
    users=(await db_session.scalars(select(User))).all()
    assert len(users)==2 and {u.role for u in users}=={'STUDIO','STUDENT'}
    assert all(u.is_verified and u.account_status=='ACTIVE' for u in users)
    for user in users:
        assert verify_password(result['credentials'][user.email],user.password_hash)
    occurrences=(await db_session.scalars(select(ClassOccurrence))).all()
    assert len(occurrences)==7
    assert {o.actual_start_at.date().isoformat() for o in occurrences} >= {'2026-10-28','2026-10-29','2026-10-24'}
    assert await db_session.scalar(select(func.count()).select_from(AttendanceRecord))==3
    assert (await db_session.scalars(select(UserCommunicationPreference.email_opted_out))).all()==[False,False]
    prefs=await db_session.scalar(select(OrganizationSetting))
    prefs.zoom_auto_meeting_enabled=True
    hashes={u.id:u.password_hash for u in users}
    await db_session.commit()
    again=await seed(db_session,'local','teacher@example.com','student@example.com',known_credentials=result['credentials'],now=now)
    await db_session.commit()
    assert again['occurrence_ids']==result['occurrence_ids'] and again['credentials']==result['credentials']
    assert await db_session.scalar(select(func.count()).select_from(ClassOccurrence))==7
    assert all(u.password_hash==hashes[u.id] for u in users)
    assert prefs.zoom_auto_meeting_enabled is True
    assert await db_session.scalar(select(func.count()).select_from(MeetingEmailDelivery))==0


@pytest.mark.asyncio
async def test_fixture_preserves_conflicting_account_role(db_session):
    db_session.add(User(email='teacher@example.com',full_name='Existing student',role='STUDENT',is_active=True,account_status='ACTIVE'))
    await db_session.commit()
    with pytest.raises(ValueError,match='conflicting role'):
        await seed(db_session,'uat','teacher@example.com','student@example.com')
