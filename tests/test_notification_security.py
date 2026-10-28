"""HTML parsing regressions: untrusted text must never become email markup."""
import inspect
from html import unescape
from html.parser import HTMLParser
from unittest.mock import AsyncMock

import httpx
import pytest
from app.common.email_content import email_href
from app.core.config import settings
from app.core.email_service import _generate_otp_email_html, send_otp_email
from app.modules.communication import templates


class EmailHTML(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.links = []
        self.tags = []
        self.events = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, value in attrs:
            if name.startswith('on'):
                self.events.append((name, value))
            if name == 'href':
                self.links.append(value)


RENDERERS = [value for name, value in vars(templates).items()
             if name.startswith('render_') and name.endswith('_email')]


def arguments(renderer, value):
    args = {}
    for name in inspect.signature(renderer).parameters:
        if name.endswith('_url'):
            # An actual quote is safe in a query only after attribute escaping.
            args[name] = 'https://studio.example/portal?offering="sample"&source=email'
        elif name == 'amount_inr':
            args[name] = 1234.5
        else:
            args[name] = value
    return args


@pytest.mark.parametrize('renderer', RENDERERS, ids=lambda f: f.__name__)
@pytest.mark.parametrize('payload', ['<a/href="https://evil.example">Pay here</a>',
                                   '"><img/src=x/onerror=alert(1)>',
                                   '</title><script>alert(1)</script>',
                                   'Yoga & <wellness> "🌿"'])
def test_every_html_notification_escapes_all_interpolated_text(renderer, payload):
    args = arguments(renderer, payload)
    subject, html = renderer(**args)
    document = EmailHTML(html)
    assert 'script' not in document.tags and 'img' not in document.tags
    assert not document.events
    expected_links = [value for key, value in args.items() if key.endswith('_url')]
    assert document.links == expected_links
    assert payload not in html
    assert payload in unescape(html)
    assert '\r' not in subject and '\n' not in subject


@pytest.mark.parametrize('renderer', RENDERERS, ids=lambda f: f.__name__)
def test_whitespace_recipient_and_header_newlines(renderer):
    args = arguments(renderer, 'Name\r\nBcc:intruder@example.com')
    for key in ('student_name', 'recipient_name'):
        if key in args:
            args[key] = '  \t '
    subject, html = renderer(**args)
    assert '\r' not in subject and '\n' not in subject
    assert 'Hello Student' in html or 'Dear Partner' in html


@pytest.mark.parametrize('value', ['javascript:alert(1)', 'data:text/html,<h1>Pay</h1>',
    '//evil.example/path', '/relative', 'https://user:password@example.com/x',
    'https://example.com/\r\nBcc:evil', 'https://example.com\\@evil.example',
    'https://example.com:99999/x', 'https://exa"mple.com/x', 'http://evil.example/x'])
def test_email_links_reject_unsafe_destinations(value):
    with pytest.raises(ValueError):
        email_href(value)


def test_local_http_links_only_in_local_environments(monkeypatch):
    assert email_href('http://localhost:3000/student')
    monkeypatch.setattr(settings, 'APP_ENV', 'uat')
    with pytest.raises(ValueError):
        email_href('http://localhost:3000/student')
    assert email_href('https://uat.progressrooms.com/student?a=1&b=2').endswith('a=1&amp;b=2')


def test_otp_template_escapes_name_code_and_purpose():
    html = _generate_otp_email_html('<img/src=x/onerror=alert(1)>', '<a/href=https://evil.example>Hi</a>', '</p><script>bad</script>')
    doc = EmailHTML(html)
    assert not doc.links and not doc.events
    assert 'img' not in doc.tags and 'script' not in doc.tags
    assert '&lt;img' in html and '&lt;script' in html
    assert 'Hello Student' in _generate_otp_email_html('123456', '   ', 'Login')


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [201, 401, 400, 500, 'exception'])
async def test_otp_provider_paths_never_log_codes_or_reflected_provider_data(monkeypatch, caplog, status):
    import logging
    monkeypatch.setattr(settings, 'BREVO_ENABLED', True)
    monkeypatch.setattr(settings, 'BREVO_API_KEY', 'synthetic-provider-key')
    async def dispatch(*args, **kwargs):
        if status == 'exception':
            raise httpx.RequestError('sensitive 918273 recipient@example.com')
        return httpx.Response(status, json={'message': 'sensitive 918273 recipient@example.com', 'messageId': '918273'})
    monkeypatch.setattr(httpx.AsyncClient, 'post', dispatch)
    with caplog.at_level(logging.INFO):
        result = await send_otp_email('recipient@example.com', 'Name', '918273')
    assert result['success'] is (status == 201)
    assert caplog.records
    assert '918273' not in caplog.text and 'recipient@example.com' not in caplog.text
    assert 'otp_code' not in result


@pytest.mark.asyncio
async def test_test_notification_escapes_custom_note_and_admin_name(db_session, monkeypatch):
    from app.models.user import User
    from app.modules.communication import service
    from app.modules.communication.schemas import CommunicationTestPayload
    admin = User(email='mail-test@example.com', full_name='<img/src=x/onerror=bad()>',
                 role='SUPER_ADMIN', is_superadmin=True, is_verified=True)
    db_session.add(admin)
    await db_session.commit()
    setting = await service.get_or_create_settings(db_session)
    setting.email_enabled = True
    await db_session.commit()
    monkeypatch.setattr(settings, 'BREVO_API_KEY', 'synthetic-provider-key')
    send = AsyncMock(return_value=(201, {'messageId': 'mail-1'}, ''))
    monkeypatch.setattr(service, '_dispatch_brevo_request', send)
    result = await service.send_test_message(db_session, admin, CommunicationTestPayload(
        channel='EMAIL', recipient='recipient@example.com', message='<a href="https://evil.example">bad</a>\nsecond line'))
    assert result.success
    html = send.call_args.kwargs['payload']['htmlContent']
    doc = EmailHTML(html)
    assert not doc.events and not doc.links and 'img' not in doc.tags
    assert '&lt;a' in html and '<br>' in html
