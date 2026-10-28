"""Bound raw webhook input before hashing, decoding or allocating a full body."""
import re
from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect

WEBHOOK_MAX_BYTES = 256 * 1024


async def read_webhook_body(request: Request) -> bytes:
    encoding = request.headers.get('content-encoding', 'identity').strip().lower()
    if encoding != 'identity':
        raise HTTPException(415, 'Encoded webhook bodies are not supported')
    lengths = request.headers.getlist('content-length')
    if len(lengths) > 1 or (lengths and not re.fullmatch(r'[0-9]+', lengths[0])):
        raise HTTPException(400, 'Invalid webhook Content-Length')
    if lengths and (len(lengths[0]) > 20 or int(lengths[0]) > WEBHOOK_MAX_BYTES):
        raise HTTPException(413, 'Webhook payload too large')
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(chunk) > WEBHOOK_MAX_BYTES - len(body):
                raise HTTPException(413, 'Webhook payload too large')
            body.extend(chunk)
    except ClientDisconnect:
        raise HTTPException(400, 'Incomplete webhook payload') from None
    if lengths and len(body) != int(lengths[0]):
        raise HTTPException(400, 'Webhook Content-Length mismatch')
    return bytes(body)
