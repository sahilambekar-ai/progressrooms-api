"""Low-cardinality request events: no bodies, raw paths, query strings or identities."""

import logging
import time
import uuid

from fastapi import Request
from starlette.responses import JSONResponse

from app.modules.admin.health import counts

log = logging.getLogger("progressrooms.requests")
log.setLevel(logging.INFO)
if not log.handlers:
    log.addHandler(logging.StreamHandler())


def response_headers(response, request_id):
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    return response


async def request_metrics(request: Request, call_next):
    request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.monotonic()
    code = 500
    try:
        try:
            response = await call_next(request)
        except Exception as error:
            response = await unhandled_error(request, error)
        code = response.status_code
        return response_headers(response, request_id)
    finally:
        elapsed = (time.monotonic() - started) * 1000
        counts[0] += 1
        counts[1] += int(code >= 500)
        counts[2] += elapsed
        route = getattr(request.scope.get("route"), "path", "unmatched")
        log.info(
            "request_completed request_id=%s method=%s route=%s status=%s duration_ms=%.1f",
            request_id,
            request.method,
            route,
            code,
            elapsed,
        )


async def unhandled_error(request: Request, error: Exception):
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
    log.error(
        "request_failed request_id=%s error_type=%s", request_id, type(error).__name__
    )
    return response_headers(
        JSONResponse(
            {"detail": "Internal server error", "request_id": request_id},
            status_code=500,
        ),
        request_id,
    )
