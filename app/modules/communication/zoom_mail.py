"""Zoom Mail transport. Never falls back to the platform transactional sender."""
import base64
from email.message import EmailMessage
from email.policy import SMTP
from app.modules.zoom.provider import ZoomProvider, ProviderError
from app.modules.zoom.service import access_token

SEND_SCOPES = {"email:write:send_msg", "email:write:send_msg:admin", "mail:write", "mail:write:admin"}


async def send_zoom_email(db, connection, recipient, subject, text, message_id):
    if connection.mode == "mock":
        # A development simulation must never be reported as a real delivery.
        return {"id": "simulated-" + str(message_id), "simulated": True}
    granted = set((connection.scopes or "").split())
    if not SEND_SCOPES.intersection(granted):
        raise ProviderError("Zoom Mail permission is missing. Add email:write:send_msg and reconnect the host's Zoom account.")
    token = await access_token(db, connection)
    mail = EmailMessage(policy=SMTP)
    mail["From"] = connection.email
    mail["To"] = recipient
    mail["Subject"] = " ".join(subject.splitlines())
    mail["Message-ID"] = f"<meeting-{message_id}@progressrooms.com>"
    mail.set_content(text)
    raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
    provider = ZoomProvider(connection.mode)
    try:
        result = await provider.request(
            "POST", "emails/mailboxes/me/messages/send", token=token, payload={"raw": raw}, ambiguous=True)
    except ProviderError as error:
        if error.status_code == 401:
            # Mailbox authorization can fail while the same OAuth grant still
            # works for meetings. Only discard credentials after confirming
            # that the identity endpoint also rejects them.
            try:
                await provider.request("GET", "users/me", token=token)
            except ProviderError as identity_error:
                if identity_error.invalid_grant:
                    raise identity_error from error
                raise ProviderError("Zoom Mail authorization failed; could not verify the meeting connection. Check Zoom Mail Service setup.", status_code=401) from error
            raise ProviderError("Zoom Mail authorization failed. Verify the Business-or-higher license, provisioned Zoom Mail Service mailbox, completed onboarding, disabled Mail end-to-end encryption, and email send permission. The meeting connection remains active.", status_code=401) from error
        raise
    if not result.get("id"):
        raise ProviderError("Zoom Mail returned no delivery reference; verify the mailbox before retrying.", ambiguous=True)
    return result
