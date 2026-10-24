"""Teaching membership is independent of permission role and Zoom authorization."""
import uuid
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from unittest.mock import AsyncMock
from app.models.organization import Organization, OrganizationMember
from app.models.user import User
from app.modules.auth.service import AuthService
from app.modules.admin.router import toggle_instructor_status
from app.modules.teachers.router import (
    list_studio_teachers, create_studio_teacher, update_studio_teacher, remove_studio_teacher,
    TeacherCreateRequest, TeacherUpdateRequest,
)


async def roster(db, owner, org):
    return await list_studio_teachers(studio_id=str(org.id), search=None, page=1, page_size=100,
                                     sort_by="name", sort_order="asc", current_user=owner, db=db)


@pytest.mark.asyncio
async def test_owner_teaches_once_retains_permissions_and_needs_no_zoom(db_session, monkeypatch):
    monkeypatch.setattr("app.modules.auth.service.send_otp_email", AsyncMock())
    email = f"{uuid.uuid4()}@example.com"
    await AuthService.register_user(db_session, "Owner", email, "password123", role="STUDIO")
    owner = await db_session.scalar(select(User).where(User.email == email))
    member = await db_session.scalar(select(OrganizationMember).where(OrganizationMember.user_id == owner.id))
    org = await db_session.get(Organization, member.organization_id)
    assert member.role == owner.role == "STUDIO"
    assert member.can_teach and len(member.teacher_code) == 8
    data = await roster(db_session, owner, org)
    assert data["total"] == data["active_total"] == 1
    assert data["teachers"][0]["is_owner"] and data["teachers"][0]["can_teach"]
    for operation in (
        lambda: remove_studio_teacher(member.id, owner, db_session),
        lambda: update_studio_teacher(member.id, TeacherUpdateRequest(is_active=False, studio_password="password123"), owner, db_session),
    ):
        with pytest.raises(HTTPException) as error:
            await operation()
        assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        await toggle_instructor_status(member.id, owner, db_session)
    assert error.value.status_code == 409
    added = await create_studio_teacher(TeacherCreateRequest(name="New teacher", email=f"{uuid.uuid4()}@example.com",
                                                            studio_id=org.id), owner, db_session)
    assert added["can_teach"] and not added["is_owner"]
    data = await roster(db_session, owner, org)
    assert data["total"] == 2
    await remove_studio_teacher(uuid.UUID(added["id"]), owner, db_session)
    assert (await roster(db_session, owner, org))["active_total"] == 1
    assert owner.role == member.role == "STUDIO" and member.is_active


@pytest.mark.asyncio
async def test_last_teacher_guard_in_existing_studio_and_tenant_isolation(db_session):
    from app.core.security import get_password_hash
    owner = User(email=f"{uuid.uuid4()}@example.com", full_name="Owner", role="STUDIO", password_hash=get_password_hash("password123"))
    teacher = User(email=f"{uuid.uuid4()}@example.com", full_name="Teacher", role="TEACHER")
    org = Organization(name="Legacy", slug=str(uuid.uuid4()))
    db_session.add_all([owner, teacher, org]); await db_session.flush()
    owner_member = OrganizationMember(organization_id=org.id, user_id=owner.id, role="STUDIO", can_teach=False)
    member = OrganizationMember(organization_id=org.id, user_id=teacher.id, role="TEACHER", teacher_code="87654321")
    db_session.add_all([owner_member, member]); await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await update_studio_teacher(member.id, TeacherUpdateRequest(is_active=False, studio_password="password123"), owner, db_session)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        await remove_studio_teacher(member.id, owner, db_session)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        await toggle_instructor_status(member.id, owner, db_session)
    assert error.value.status_code == 409
    assert member.is_active
    outsider = User(email=f"{uuid.uuid4()}@example.com", full_name="Other owner", role="STUDIO")
    db_session.add(outsider); await db_session.flush()
    with pytest.raises(HTTPException) as error:
        await remove_studio_teacher(member.id, outsider, db_session)
    assert error.value.status_code == 403
