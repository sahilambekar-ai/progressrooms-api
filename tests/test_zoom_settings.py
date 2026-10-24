import uuid
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from app.models.organization import Organization, OrganizationMember
from app.modules.auth.service import AuthService
from app.modules.organizations.router import get_zoom_settings, update_zoom_settings, update_studio_profile, StudioProfileUpdate, update_zoom_host_key, ZoomHostKeyUpdateRequest
from app.modules.zoom.router import DefaultsRequest
from test_teacher_zoom import studio, zoom_config

@pytest.mark.asyncio
async def test_zoom_settings_defaults_and_updates(db_session, monkeypatch):
    monkeypatch.setattr("app.modules.auth.service.send_otp_email", AsyncMock())
    email = f"{uuid.uuid4()}@example.com"
    _, _, _ = await AuthService.register_user(db_session, "Owner", email, "password123", role="STUDIO")
    from app.models.user import User
    user = await db_session.scalar(select(User).where(User.email == email))
    member = await db_session.scalar(select(OrganizationMember).where(OrganizationMember.user_id == user.id))
    org = await db_session.get(Organization, member.organization_id)
    before = await get_zoom_settings(org.id, (org, member), db_session)
    assert before["zoom_waiting_room"] is True and before["zoom_connected"] is False
    assert "zoom_host_key" not in before
    updated = await update_zoom_settings(org.id, DefaultsRequest(version=before["version"], waiting_room=False, enforce_zoom_login=True), (org, member), db_session)
    assert updated["zoom_waiting_room"] is False and updated["zoom_enforce_zoom_login"] is True
    with pytest.raises(HTTPException) as error:
        await update_zoom_settings(org.id, DefaultsRequest(version=before["version"]), (org, member), db_session)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        await update_zoom_settings(org.id, DefaultsRequest(version=updated["version"], auto_meeting_enabled=True), (org, member), db_session)
    with pytest.raises(HTTPException):
        await update_studio_profile(org.id, StudioProfileUpdate(zoom_connected=True, zoom_account_email="forged@example.com"), (org, member), user, db_session)
    with pytest.raises(HTTPException) as error:
        await update_zoom_host_key(org.id, ZoomHostKeyUpdateRequest(host_key="123456"), (org, member), db_session)
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_onboarding_host_key_is_encrypted_and_private(db_session, studio):
    from test_teacher_zoom import authorize
    from app.modules.organizations.router import get_zoom_host_key_status
    from app.modules.zoom import service as svc
    from app.modules.zoom.provider import unseal
    from pydantic import ValidationError
    org, owner, _, member, _ = studio
    for invalid in ("12345", "abcdef", "1234567"):
        with pytest.raises(ValidationError):
            ZoomHostKeyUpdateRequest(host_key=invalid)
    await authorize(db_session, org, member, owner)
    before = await get_zoom_host_key_status(org.id, (org, member), db_session)
    assert before['host_key_configured'] is False
    saved = await update_zoom_host_key(org.id, ZoomHostKeyUpdateRequest(host_key="123456"), (org, member), db_session)
    assert saved['host_key_configured'] is True and 'host_key' not in saved
    connection = await svc.connection_for(db_session, member.id)
    assert connection.host_key_enc != "123456" and "123456" not in connection.host_key_enc
    assert unseal(connection.host_key_enc)['zoom_user_id'] == connection.zoom_user_id
    assert unseal(connection.host_key_enc)['host_key'] == "123456"
    status = await get_zoom_host_key_status(org.id, (org, member), db_session)
    assert status['host_key_configured'] is True and 'host_key' not in status
    settings = await get_zoom_settings(org.id, (org, member), db_session)
    assert 'host_key' not in str(settings)
    await svc.disconnect(db_session, member, pause_confirmed=True)
    assert connection.host_key_enc is None
