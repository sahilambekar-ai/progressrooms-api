import pytest
import pytest_asyncio
import uuid
import httpx
from datetime import datetime, timezone
from sqlalchemy import select

from app.main import app, api_v1
from app.core.database import get_db
from app.models.user import User
from app.models.communication import PlatformCommunicationSetting, CommunicationChannelAuditLog, CommunicationLog
from app.core.security import create_access_token
from app.modules.communication.service import get_or_create_settings


@pytest_asyncio.fixture
async def client(db_session):
    async def dependency():
        yield db_session
    api_v1.dependency_overrides[get_db] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1") as client:
        yield client
    api_v1.dependency_overrides.clear()


@pytest.fixture
def auth_headers():
    def _make(user: User):
        token = create_access_token({"sub": str(user.id), "role": user.role, "email": user.email})
        return {"Authorization": f"Bearer {token}"}
    return _make


async def get_or_create_superadmin(db_session):
    stmt = select(User).where(User.role == "SUPER_ADMIN")
    admin = (await db_session.execute(stmt)).scalar_one_or_none()
    if not admin:
        admin = User(
            email=f"admin_{uuid.uuid4().hex[:6]}@progressrooms.local",
            full_name="Platform Admin",
            role="SUPER_ADMIN",
            is_superadmin=True,
            is_active=True,
            is_verified=True,
        )
        db_session.add(admin)
        await db_session.commit()
    return admin


@pytest.mark.asyncio
async def test_communication_settings_rbac(db_session, client, auth_headers, monkeypatch):
    """
    Only SUPER_ADMIN can view and manage platform communication channel toggles.
    Other roles (STUDIO, TEACHER, STUDENT) must receive 403 Forbidden.
    """
    from app.core.config import settings
    monkeypatch.setattr(settings, "DEV_TEST_LOGIN_ENABLED", False)
    superadmin = await get_or_create_superadmin(db_session)
    studio_user = User(
        email=f"owner_{uuid.uuid4().hex[:6]}@studio.test",
        full_name="Studio Owner",
        role="STUDIO",
        is_active=True,
        is_verified=True,
    )
    db_session.add(studio_user)
    await db_session.commit()

    # Studio user is rejected
    res_forbidden = await client.get("/admin/communication/settings", headers=auth_headers(studio_user))
    assert res_forbidden.status_code == 403

    # Super Admin can view settings
    res_ok = await client.get("/admin/communication/settings", headers=auth_headers(superadmin))
    assert res_ok.status_code == 200
    data = res_ok.json()
    assert data["whatsapp_enabled"] is True
    assert data["email_enabled"] is True
    assert data["scope"] == "Applies to all studios"
    assert "connection_status" in data
    assert "email" in data["connection_status"]
    assert "whatsapp_admin" in data["connection_status"]
    assert "whatsapp_classes" in data["connection_status"]


@pytest.mark.asyncio
async def test_communication_channel_toggles_and_audit_logging(db_session, client, auth_headers):
    """
    Verifies that toggling WhatsApp and Email channel switches:
    1. Updates backend settings.
    2. Writes a complete audit trail record for each change.
    3. Exposes the warning flag when both channels are disabled.
    """
    superadmin = await get_or_create_superadmin(db_session)

    # Turn WhatsApp OFF
    patch1 = await client.patch(
        "/admin/communication/settings",
        json={"whatsapp_enabled": False, "notes": "Paused for carrier maintenance"},
        headers=auth_headers(superadmin),
    )
    assert patch1.status_code == 200
    d1 = patch1.json()
    assert d1["whatsapp_enabled"] is False
    assert d1["email_enabled"] is True
    assert d1["both_channels_disabled"] is False

    # Turn Email OFF as well -> both channels disabled
    patch2 = await client.patch(
        "/admin/communication/settings",
        json={"email_enabled": False, "notes": "Complete quiet hours policy test"},
        headers=auth_headers(superadmin),
    )
    assert patch2.status_code == 200
    d2 = patch2.json()
    assert d2["whatsapp_enabled"] is False
    assert d2["email_enabled"] is False
    assert d2["both_channels_disabled"] is True

    # Check audit log trail endpoint
    audit_res = await client.get("/admin/communication/audit-logs", headers=auth_headers(superadmin))
    assert audit_res.status_code == 200
    logs = audit_res.json()
    assert len(logs) >= 2
    channels_logged = [l["channel"] for l in logs[:2]]
    assert "EMAIL" in channels_logged
    assert "WHATSAPP" in channels_logged


@pytest.mark.asyncio
async def test_channel_switch_suppression_and_test_dispatch(db_session, client, auth_headers, monkeypatch):
    """
    Verifies that switching a channel off suppresses test dispatches and logs
    the exact reason ('Suppressed: email disabled' / 'Suppressed: whatsapp disabled').
    """
    superadmin = await get_or_create_superadmin(db_session)

    # Ensure both channels are OFF
    await client.patch(
        "/admin/communication/settings",
        json={"whatsapp_enabled": False, "email_enabled": False},
        headers=auth_headers(superadmin),
    )

    # 1. Dispatch test email while email is OFF -> Must be SUPPRESSED
    test_email_res = await client.post(
        "/admin/communication/test",
        json={"channel": "EMAIL", "recipient": "student@example.com"},
        headers=auth_headers(superadmin),
    )
    assert test_email_res.status_code == 200
    res_data = test_email_res.json()
    assert res_data["status"] == "SUPPRESSED"
    assert res_data["suppression_reason"] == "Suppressed: email disabled"

    # 2. Dispatch test WhatsApp while WhatsApp is OFF -> Must be SUPPRESSED
    test_wa_res = await client.post(
        "/admin/communication/test",
        json={"channel": "WHATSAPP_ADMIN", "recipient": "+919876543210"},
        headers=auth_headers(superadmin),
    )
    assert test_wa_res.status_code == 200
    res_wa_data = test_wa_res.json()
    assert res_wa_data["status"] == "SUPPRESSED"
    assert res_wa_data["suppression_reason"] == "Suppressed: whatsapp disabled"

    # Provider credentials are explicit so this test does not depend on a local .env.
    from app.core.config import settings
    monkeypatch.setattr(settings, "BREVO_API_KEY", "unit-test-key-not-a-real-credential")

    # 3. Turn Email back ON -> Test email succeeds (SENT)
    await client.patch(
        "/admin/communication/settings",
        json={"email_enabled": True},
        headers=auth_headers(superadmin),
    )
    from unittest.mock import patch, AsyncMock
    with patch("app.modules.communication.service._dispatch_brevo_request", new_callable=AsyncMock, return_value=(200, {"messageId": "test_msg_ok_123"}, "ok")) as dispatch:
        test_email_on = await client.post(
            "/admin/communication/test",
            json={"channel": "EMAIL", "recipient": "student@example.com"},
            headers=auth_headers(superadmin),
        )
    dispatch.assert_awaited_once()
    assert test_email_on.status_code == 200
    assert test_email_on.json()["status"] == "SENT"

    # 4. Check communication history contains both suppressed and sent records
    history_res = await client.get("/admin/communication/history", headers=auth_headers(superadmin))
    assert history_res.status_code == 200
    history_data = history_res.json()
    assert history_data["total_items"] >= 3
    statuses = [item["status"] for item in history_data["items"]]
    assert "SUPPRESSED" in statuses
    assert "SENT" in statuses
    suppressed_items = [item for item in history_data["items"] if item["status"] == "SUPPRESSED"]
    assert any("Suppressed: email disabled" in s["suppression_reason"] for s in suppressed_items)


@pytest.mark.asyncio
async def test_auth_emails_unaffected_by_operational_toggle(db_session, client, auth_headers):
    """
    Critical security rule: Disabling operational class/billing emails
    must NEVER suppress authentication or verification emails (login OTP, password reset).
    Users must never be locked out of their accounts.
    """
    from unittest.mock import patch, AsyncMock
    from app.modules.auth.service import AuthService

    superadmin = await get_or_create_superadmin(db_session)

    # Disable operational email switch
    await client.patch(
        "/admin/communication/settings",
        json={"email_enabled": False},
        headers=auth_headers(superadmin),
    )

    # Request an authentication OTP for a user
    test_user_email = f"user_{uuid.uuid4().hex[:6]}@example.com"
    await AuthService.register_user(
        db_session,
        "Secure Yogi",
        test_user_email,
        "securepass123",
        phone="+91 98765 11111",
        location="Mumbai",
        role="STUDENT"
    )

    mock_send = AsyncMock(return_value={"success": True, "message_id": "auth_otp_123"})
    with patch("app.modules.auth.service.send_otp_email", mock_send):
        otp_code, _, _, _, _ = await AuthService.request_otp(db_session, test_user_email)

    assert len(otp_code) == 6
    assert mock_send.called
    recipient_arg = mock_send.call_args.kwargs.get("to_email") or (mock_send.call_args[0][0] if mock_send.call_args[0] else None)
    assert recipient_arg == test_user_email

    # Restore operational switches back to True
    await client.patch(
        "/admin/communication/settings",
        json={"email_enabled": True, "whatsapp_enabled": True},
        headers=auth_headers(superadmin),
    )



@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["EMAIL", "WHATSAPP_ADMIN", "WHATSAPP_CLASSES"])
async def test_simulated_tests_do_not_claim_delivery(db_session, client, auth_headers, monkeypatch, channel):
    from app.core.config import settings
    monkeypatch.setattr(settings, "BREVO_API_KEY", "")
    admin = await get_or_create_superadmin(db_session)
    headers = auth_headers(admin)
    await client.patch("/admin/communication/settings", headers=headers,
                       json={"email_enabled": True, "whatsapp_enabled": True})
    response = await client.post("/admin/communication/test", headers=headers,
                                 json={"channel": channel, "recipient": "test@example.com" if channel == "EMAIL" else "+919876543210"})
    assert response.status_code == 200
    assert response.json()["status"] == "SIMULATED"
    assert response.json()["success"] is False
    history = await client.get("/admin/communication/history", headers=headers)
    assert history.status_code == 200
    assert history.json()["items"][0]["status"] == "SIMULATED"


@pytest.mark.asyncio
async def test_brevo_rejection_reports_provider_error(db_session, client, auth_headers, monkeypatch):
    from unittest.mock import AsyncMock
    from app.core.config import settings
    from app.modules.communication import service
    monkeypatch.setattr(settings, "BREVO_API_KEY", "test-provider-key")
    monkeypatch.setattr(service, "_dispatch_brevo_request", AsyncMock(return_value=(400, {}, "Invalid sender")))
    admin = await get_or_create_superadmin(db_session)
    headers = auth_headers(admin)
    await client.patch("/admin/communication/settings", headers=headers, json={"email_enabled": True})
    response = await client.post("/admin/communication/test", headers=headers,
                                 json={"channel": "EMAIL", "recipient": "test@example.com"})
    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    assert response.json()["details"] == "Brevo HTTP error: Invalid sender"


@pytest.mark.asyncio
@pytest.mark.parametrize("dev_enabled", [True, False])
async def test_studio_communication_testing_access(db_session, client, auth_headers, monkeypatch, dev_enabled):
    from app.core.config import settings
    monkeypatch.setattr(settings, "DEV_TEST_LOGIN_ENABLED", dev_enabled)
    monkeypatch.setattr(settings, "BREVO_API_KEY", "")
    studio = User(email=f"tester_{uuid.uuid4().hex[:8]}@studio.test", full_name="Studio Tester",
                  role="STUDIO", is_active=True, is_verified=True)
    db_session.add(studio)
    await db_session.commit()
    headers = auth_headers(studio)
    expected = 200 if dev_enabled else 403
    for path in ("settings", "audit-logs", "history"):
        result = await client.get(f"/admin/communication/{path}", headers=headers)
        assert result.status_code == expected
    result = await client.patch("/admin/communication/settings", headers=headers,
                                json={"email_enabled": False})
    assert result.status_code == expected
    result = await client.post("/admin/communication/test", headers=headers,
                               json={"channel": "EMAIL", "recipient": studio.email})
    assert result.status_code == expected
    if dev_enabled:
        assert result.json()["status"] == "SUPPRESSED"
        logs = (await client.get("/admin/communication/audit-logs", headers=headers)).json()
        assert logs[0]["changed_by_email"] == studio.email
    # Local studio testing must not unlock budget administration or engine execution.
    assert (await client.get("/admin/communication/budgets", headers=headers)).status_code == 403
    assert (await client.post("/admin/communication/run-engine-cycle", headers=headers)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [None, "STUDENT", "TEACHER"])
async def test_development_testing_still_requires_studio_login(db_session, client, auth_headers, monkeypatch, role):
    from app.core.config import settings
    monkeypatch.setattr(settings, "DEV_TEST_LOGIN_ENABLED", True)
    headers = {}
    if role:
        user = User(email=f"restricted_{uuid.uuid4().hex[:8]}@test.local", full_name="Restricted User",
                    role=role, is_active=True, is_verified=True)
        db_session.add(user)
        await db_session.commit()
        headers = auth_headers(user)
    response = await client.get("/admin/communication/settings", headers=headers)
    assert response.status_code == (401 if role is None else 403)


@pytest.mark.asyncio
async def test_test_email_without_provider_key_is_simulated(db_session, client, auth_headers, monkeypatch):
    from app.core.config import settings
    from unittest.mock import patch, AsyncMock
    monkeypatch.setattr(settings, "BREVO_API_KEY", "")
    admin = await get_or_create_superadmin(db_session)
    headers = auth_headers(admin)
    await client.patch("/admin/communication/settings", json={"email_enabled": True}, headers=headers)
    with patch("app.modules.communication.service._dispatch_brevo_request", new_callable=AsyncMock) as dispatch:
        response = await client.post("/admin/communication/test", json={"channel": "EMAIL", "recipient": "student@example.com"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "SIMULATED"
    assert response.json()["success"] is False
    dispatch.assert_not_awaited()
