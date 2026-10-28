"""Offline authentication audit regressions."""
from datetime import timedelta
from unittest.mock import AsyncMock
import uuid

import httpx
import pytest
import pytest_asyncio

from app.core.database import get_db
from app.core.security import create_access_token
from app.main import app, api_v1
from app.models.user import User
from app.modules.auth import service


@pytest_asyncio.fixture
async def audit_client(db_session, monkeypatch):
    monkeypatch.setattr(service, "send_otp_email", AsyncMock(return_value=True))
    async def dependency():
        yield db_session
    previous = api_v1.dependency_overrides.copy()
    api_v1.dependency_overrides[get_db] = dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://audit/api/v1/") as client:
            yield client
    finally:
        api_v1.dependency_overrides.clear()
        api_v1.dependency_overrides.update(previous)


async def student(db):
    user = User(email=f"audit-{uuid.uuid4().hex}@example.com", full_name="Audit छात्र",
                role="STUDENT", is_verified=True, is_active=True, account_status="ACTIVE")
    db.add(user)
    await db.commit()
    return user


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "invalid", create_access_token({"sub": "bad-uuid"}),
    create_access_token({"sub": str(uuid.uuid4())}, timedelta(seconds=-1)),
    create_access_token({"sub": str(uuid.uuid4())})])
async def test_invalid_auth_is_rejected(audit_client, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = await audit_client.get("auth/me", headers=headers)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_reset_code_expires(audit_client, db_session):
    from app.models.otp import UserOtp
    from sqlalchemy import select
    from datetime import datetime, timezone
    user = await student(db_session)
    code, _, _ = await service.AuthService.forgot_password(db_session, user.email)
    record = await db_session.scalar(select(UserOtp).where(UserOtp.user_id == user.id))
    record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    response = await audit_client.post("auth/reset-password", json={
        "email": user.email, "code": code, "new_password": "replacement-password"})
    assert response.status_code == 400
    assert "expired" in response.json()["detail"]


@pytest.mark.asyncio
async def test_reset_code_cannot_be_reused(audit_client, db_session):
    user = await student(db_session)
    code, _, _ = await service.AuthService.forgot_password(db_session, user.email)
    payload = {"email": user.email, "code": code, "new_password": "replacement-password"}
    assert (await audit_client.post("auth/reset-password", json=payload)).status_code == 200
    assert (await audit_client.post("auth/reset-password", json=payload)).status_code == 400


@pytest.mark.asyncio
async def test_password_reset_code_cannot_issue_login_token(audit_client, db_session):
    user = await student(db_session)
    code, _, _ = await service.AuthService.forgot_password(db_session, user.email)
    response = await audit_client.post("auth/verify-otp", json={"email": user.email, "otp": code})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_password_reset_preserves_suspension(audit_client, db_session):
    user = await student(db_session)
    user.account_status = "SUSPENDED"
    await db_session.commit()
    code, _, _ = await service.AuthService.forgot_password(db_session, user.email)
    await audit_client.post("auth/reset-password", json={
        "email": user.email, "code": code, "new_password": "replacement-password"})
    await db_session.refresh(user)
    assert user.account_status == "SUSPENDED"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,verified,active,deleted", [
    ("ACTIVE", True, True, False),
    ("DISABLED", True, True, False),
    ("PENDING_VERIFICATION", False, True, False),
    ("ACTIVE", True, False, False),
    ("ACTIVE", True, True, True),
])
async def test_reset_preserves_account_lifecycle(audit_client, db_session, status, verified, active, deleted):
    from datetime import datetime, timezone
    from app.core.security import verify_password
    user = await student(db_session)
    user.account_status, user.is_verified, user.is_active = status, verified, active
    user.deleted_at = datetime.now(timezone.utc) if deleted else None
    await db_session.commit()
    code, _, _ = await service.AuthService.forgot_password(db_session, user.email)
    response = await audit_client.post("auth/reset-password", json={
        "email": user.email, "code": code, "new_password": "replacement-password"})
    assert response.status_code == 200
    await db_session.refresh(user)
    assert (user.account_status, user.is_verified, user.is_active, bool(user.deleted_at)) == (status, verified, active, deleted)
    assert verify_password("replacement-password", user.password_hash)
    login = await audit_client.post("auth/login", json={"email": user.email, "password": "replacement-password"})
    if status == "ACTIVE" and active and not deleted:
        assert login.status_code == 200
        assert login.json()["access_token"]
    else:
        assert not login.json().get("access_token")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["auth/request-otp", "auth/otp/generate", "auth/verify-otp", "auth/otp/verify"])
@pytest.mark.parametrize("purpose", ["PASSWORD_RESET", "UNKNOWN"])
async def test_login_endpoints_reject_other_purposes(audit_client, db_session, path, purpose):
    user = await student(db_session)
    response = await audit_client.post(path, json={"email": user.email, "otp": "123456", "purpose": purpose})
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["LOGIN_VERIFICATION", "EMAIL_VERIFICATION"])
async def test_login_and_activation_codes_remain_compatible(audit_client, db_session, purpose):
    from app.models.otp import UserOtp
    user = await student(db_session)
    user.is_verified = False
    user.account_status = "PENDING_VERIFICATION"
    await db_session.commit()
    code, record_id, *_ = await service.AuthService.request_otp(db_session, user.email, purpose=purpose)
    response = await audit_client.post("auth/verify-otp", json={"email": user.email, "otp": code})
    assert response.status_code == 200
    assert response.json()["access_token"]
    assert (await db_session.get(UserOtp, uuid.UUID(record_id))).verified_at


@pytest.mark.asyncio
async def test_login_request_does_not_consume_reset_code(audit_client, db_session):
    user = await student(db_session)
    reset_code, _, _ = await service.AuthService.forgot_password(db_session, user.email)
    await service.AuthService.request_otp(db_session, user.email)
    response = await audit_client.post("auth/reset-password", json={
        "email": user.email, "code": reset_code, "new_password": "replacement-password"})
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_undelivered_otp_is_not_logged(monkeypatch, caplog, enabled):
    import logging
    from app.core.config import settings
    from app.core.email_service import send_otp_email
    monkeypatch.setattr(settings, "BREVO_ENABLED", enabled)
    monkeypatch.setattr(settings, "BREVO_API_KEY", "")
    with caplog.at_level(logging.INFO):
        result = await send_otp_email(to_email="audit-recipient@example.com", to_name="Audit", otp_code="918273")
    assert result["success"] is False
    assert caplog.records
    assert "918273" not in caplog.text
    assert "audit-recipient@example.com" not in caplog.text


@pytest.mark.asyncio
async def test_reset_enforces_registration_password_minimum(audit_client, db_session):
    user = await student(db_session)
    code, _, _ = await service.AuthService.forgot_password(db_session, user.email)
    response = await audit_client.post("auth/reset-password", json={
        "email": user.email, "code": code, "new_password": "sixsix"})
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("password", ["a" * 72 + "first", "界" * 24 + "first"])
async def test_distinct_long_passwords_do_not_authenticate_each_other(audit_client, password):
    from app.core.security import get_password_hash, verify_password
    assert not verify_password(password + "different", get_password_hash(password))
