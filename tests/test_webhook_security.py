"""Bound every webhook before HMAC/JSON work, including chunked or lying input."""
import hashlib
import hmac
import json
import time
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.common.webhook_body import WEBHOOK_MAX_BYTES, read_webhook_body
from app.core.config import settings
from test_full_audit_auth import audit_client  # noqa: F401

PATHS = ['commerce/webhooks/razorpay', 'saas-billing/webhooks/razorpay', 'integrations/zoom/deauthorization']
SECRET = 'synthetic-webhook-signing-value'


@pytest.fixture(autouse=True)
def webhook_keys(monkeypatch):
    monkeypatch.setattr(settings, 'RAZORPAY_WEBHOOK_SECRET', SECRET)
    monkeypatch.setattr(settings, 'SAAS_RAZORPAY_WEBHOOK_SECRET', SECRET)
    monkeypatch.setattr(settings, 'ZOOM_WEBHOOK_SECRET', SECRET)


def signed(path, body):
    if path.startswith('integrations/'):
        stamp = str(int(time.time()))
        digest = hmac.new(SECRET.encode(), b'v0:' + stamp.encode() + b':' + body, hashlib.sha256).hexdigest()
        return {'x-zm-signature': 'v0=' + digest, 'x-zm-request-timestamp': stamp}
    return {'x-razorpay-signature': hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()}


@pytest.mark.asyncio
@pytest.mark.parametrize('path', PATHS)
async def test_declared_oversize_is_rejected_before_signature_work(audit_client, path):
    with patch('hmac.new', side_effect=AssertionError('Signature must not be computed')):
        response = await audit_client.post(path, content=b'', headers={'content-length': str(WEBHOOK_MAX_BYTES + 1)})
    assert response.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize('path', PATHS)
async def test_chunked_oversize_is_rejected_without_declared_length(audit_client, path):
    async def chunks():
        yield b'x' * (WEBHOOK_MAX_BYTES // 2)
        yield b'x' * (WEBHOOK_MAX_BYTES // 2)
        yield b'x'
        raise AssertionError('Reader should stop immediately when the limit is exceeded')
    with patch('hmac.new', side_effect=AssertionError('Signature must not be computed')):
        response = await audit_client.post(path, content=chunks())
    assert response.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize('path', PATHS)
async def test_false_small_content_length_cannot_bypass_stream_cap(audit_client, path):
    with patch('hmac.new', side_effect=AssertionError('Signature must not be computed')):
        response = await audit_client.post(path, content=b'x' * (WEBHOOK_MAX_BYTES + 1), headers={'content-length': '1'})
    assert response.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize('path', PATHS)
async def test_exact_limit_signed_payload_still_works(audit_client, path):
    payload = {'event': 'unused.event', 'payload': {}}
    body = json.dumps(payload).encode()
    body += b' ' * (WEBHOOK_MAX_BYTES - len(body))
    response = await audit_client.post(path, content=body, headers=signed(path, body))
    assert response.status_code == 200, response.text
    # Existing payment ledgers remain idempotent; Zoom ignores this event.
    repeated = await audit_client.post(path, content=body, headers=signed(path, body))
    assert repeated.status_code == 200
    if path.startswith('commerce/'):
        assert repeated.json()['status'] == 'already_processed'
    elif path.startswith('saas-'):
        assert repeated.json()['status'] == 'IGNORED'


@pytest.mark.asyncio
@pytest.mark.parametrize('path', PATHS)
async def test_under_limit_bad_signature_still_fails(audit_client, path):
    response = await audit_client.post(path, content=b'{}', headers={'x-zm-request-timestamp': str(int(time.time()))})
    assert response.status_code == (401 if path.startswith('integrations/') else 400)


@pytest.mark.asyncio
@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('body', [b'[]', b'null', b'{"payload":null}', b'{"payload":[]}', b'{', b'[' * 2000 + b']' * 2000])
async def test_signed_invalid_json_or_shape_returns_400(audit_client, path, body):
    response = await audit_client.post(path, content=body, headers=signed(path, body))
    if path.startswith('saas-') and body in [b'{"payload":null}', b'{"payload":[]}']:
        # SaaS intentionally ignores non-payment events regardless of payload.
        assert response.status_code == 200
    else:
        assert response.status_code == 400, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('headers,status', [({'content-length': '-1'}, 400),
    ({'content-length': 'not-a-number'}, 400), ({'content-length': '0'}, 400),
    ({'content-encoding': 'gzip'}, 415)])
async def test_malformed_lengths_and_encoded_bodies_are_rejected(audit_client, path, headers, status):
    response = await audit_client.post(path, content=b'{}', headers=headers)
    assert response.status_code == status


def raw_request(messages, headers=()):
    seen = []
    async def receive():
        message = messages[len(seen)]
        seen.append(message)
        return message
    request = Request({'type': 'http', 'method': 'POST', 'path': '/hook', 'headers': list(headers)}, receive)
    return request, seen


@pytest.mark.asyncio
async def test_declared_overflow_never_reads_stream():
    request, seen = raw_request([], [(b'content-length', str(WEBHOOK_MAX_BYTES + 1).encode())])
    with pytest.raises(HTTPException) as error:
        await read_webhook_body(request)
    assert error.value.status_code == 413 and not seen


@pytest.mark.asyncio
async def test_reader_stops_on_overflow_and_rejects_duplicate_lengths():
    request, seen = raw_request([
        {'type': 'http.request', 'body': b'x' * WEBHOOK_MAX_BYTES, 'more_body': True},
        {'type': 'http.request', 'body': b'x', 'more_body': True},
    ])
    with pytest.raises(HTTPException) as error:
        await read_webhook_body(request)
    assert error.value.status_code == 413 and len(seen) == 2
    request, seen = raw_request([], [(b'content-length', b'1'), (b'content-length', b'1')])
    with pytest.raises(HTTPException) as error:
        await read_webhook_body(request)
    assert error.value.status_code == 400 and not seen


@pytest.mark.asyncio
async def test_disconnected_body_is_a_client_error():
    request, _ = raw_request([{'type': 'http.disconnect'}])
    with pytest.raises(HTTPException) as error:
        await read_webhook_body(request)
    assert error.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize('path', PATHS)
async def test_non_ascii_signature_is_rejected_as_client_error(audit_client, path):
    headers = [(b'x-razorpay-signature', b'\xff'), (b'x-zm-signature', b'\xff'),
               (b'x-zm-request-timestamp', str(int(time.time())).encode())]
    response = await audit_client.post(path, content=b'{}', headers=headers)
    assert response.status_code == (401 if path.startswith('integrations/') else 400)
