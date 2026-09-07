import pytest
import uuid
from app.models.organization import Organization, OrganizationMember
from app.models.session import ClassType, Session
from app.models.user import User
from sqlalchemy import select

@pytest.mark.asyncio
async def test_cross_tenant_isolation(db_session):
    # Tenant 1: Yoga Studio
    org1 = Organization(name="Yoga Tenant", slug="yoga-tenant")
    db_session.add(org1)
    await db_session.flush()

    ct1 = ClassType(organization_id=org1.id, name="Yoga", slug="yoga")
    db_session.add(ct1)
    await db_session.flush()

    s1 = Session(organization_id=org1.id, class_type_id=ct1.id, name="Secret Yoga Class", slug="secret-yoga")
    db_session.add(s1)

    # Tenant 2: Music Academy
    org2 = Organization(name="Music Tenant", slug="music-tenant")
    db_session.add(org2)
    await db_session.flush()

    ct2 = ClassType(organization_id=org2.id, name="Music", slug="music")
    db_session.add(ct2)
    await db_session.flush()

    s2 = Session(organization_id=org2.id, class_type_id=ct2.id, name="Secret Guitar Class", slug="secret-guitar")
    db_session.add(s2)

    await db_session.commit()

    # Query scoped to org1 should NEVER return sessions from org2
    stmt1 = select(Session).where(Session.organization_id == org1.id)
    res1 = await db_session.execute(stmt1)
    org1_sessions = res1.scalars().all()

    assert len(org1_sessions) == 1
    assert org1_sessions[0].name == "Secret Yoga Class"
    assert all(s.organization_id == org1.id for s in org1_sessions)

    # Query scoped to org2
    stmt2 = select(Session).where(Session.organization_id == org2.id)
    res2 = await db_session.execute(stmt2)
    org2_sessions = res2.scalars().all()

    assert len(org2_sessions) == 1
    assert org2_sessions[0].name == "Secret Guitar Class"
    assert all(s.organization_id == org2.id for s in org2_sessions)
