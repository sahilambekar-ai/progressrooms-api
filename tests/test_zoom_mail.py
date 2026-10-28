import base64
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.modules.communication.zoom_mail import send_zoom_email
from app.modules.zoom.provider import ProviderError


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["email:write:send_msg", "email:write:send_msg:admin"])
async def test_zoom_mail_posts_one_recipient_rfc_message(monkeypatch, scope):
    transport = AsyncMock(return_value={"id": "zoom-sent-id"})
    monkeypatch.setattr("app.modules.communication.zoom_mail.access_token", AsyncMock(return_value="unit-test-token"))
    monkeypatch.setattr("app.modules.communication.zoom_mail.ZoomProvider", lambda mode: SimpleNamespace(request=transport))
    conn = SimpleNamespace(mode="live", scopes=scope, email="host@example.test")
    result = await send_zoom_email(None, conn, "student@example.test", "Class reminder: Yoga", "Join your class: https://example.test/meetings/123", "delivery-123")
    assert result["id"] == "zoom-sent-id"
    args, kwargs = transport.await_args
    assert args == ("POST", "emails/mailboxes/me/messages/send")
    assert kwargs["ambiguous"] is True
    assert set(kwargs["payload"]) == {"raw"}  # Timing is owned by our queue, so cancellation is local.
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(kwargs["payload"]["raw"]))
    assert message["From"] == "host@example.test"
    assert message["To"] == "student@example.test"
    assert message["Bcc"] is None and message["Cc"] is None
    assert message["Message-ID"] == "<meeting-delivery-123@progressrooms.com>"
    assert "https://example.test/meetings/123" in message.get_content()


@pytest.mark.asyncio
async def test_missing_mail_scope_never_calls_a_provider(monkeypatch):
    token = AsyncMock()
    monkeypatch.setattr("app.modules.communication.zoom_mail.access_token", token)
    with pytest.raises(ProviderError, match="permission is missing"):
        await send_zoom_email(None, SimpleNamespace(mode="live", scopes="meeting:read:meeting"), "student@example.test", "Reminder", "Text", "id")
    token.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_zoom_response_is_uncertain(monkeypatch):
    monkeypatch.setattr("app.modules.communication.zoom_mail.access_token", AsyncMock(return_value="unit-test-token"))
    monkeypatch.setattr("app.modules.communication.zoom_mail.ZoomProvider", lambda mode: SimpleNamespace(request=AsyncMock(return_value={})))
    with pytest.raises(ProviderError) as error:
        await send_zoom_email(None, SimpleNamespace(mode="live", scopes="email:write:send_msg", email="host@example.test"), "student@example.test", "Reminder", "Text", "id")
    assert error.value.ambiguous is True


@pytest.mark.asyncio
async def test_mock_zoom_mail_is_explicitly_simulated(monkeypatch):
    token = AsyncMock()
    monkeypatch.setattr("app.modules.communication.zoom_mail.access_token", token)
    result = await send_zoom_email(None, SimpleNamespace(mode="mock"), "student@example.test", "Reminder", "Text", "id")
    assert result["simulated"] is True
    token.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_outcome, invalid_grant", [({"id": "host"}, False), (ProviderError(invalid_grant=True, status_code=401), True), (ProviderError(status_code=503), False)])
async def test_mailbox_rejection_only_invalidates_a_confirmed_invalid_identity(monkeypatch, identity_outcome, invalid_grant):
    transport = AsyncMock(side_effect=[ProviderError(invalid_grant=True, status_code=401), identity_outcome])
    monkeypatch.setattr("app.modules.communication.zoom_mail.access_token", AsyncMock(return_value="unit-test-token"))
    monkeypatch.setattr("app.modules.communication.zoom_mail.ZoomProvider", lambda mode: SimpleNamespace(request=transport))
    with pytest.raises(ProviderError) as error:
        await send_zoom_email(None, SimpleNamespace(mode="live", scopes="email:write:send_msg", email="host@example.test"), "student@example.test", "Reminder", "Text", "id")
    assert error.value.invalid_grant is invalid_grant
    assert transport.await_args_list[1].args == ("GET", "users/me")
