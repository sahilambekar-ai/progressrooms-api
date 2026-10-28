"""Shared, expiring fixed-window quotas; no in-process or plaintext PII keys."""
import hashlib
import hmac
import ipaddress
import time
from fastapi import Depends, HTTPException, Request
from sqlalchemy import case, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import get_db
from app.models.auth_rate_limit import AuthRateLimit


def _key(scope: str, subject: str, seconds: int) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f'{scope}:{seconds}:{subject}'.encode(), hashlib.sha256).hexdigest()


async def consume(db: AsyncSession, rules: list[tuple[str, str, int, int]]) -> None:
    """Commit quotas before auth work so invalid credentials/rollbacks cannot erase them.

    Atomic upserts serialize workers. Counts saturate at limit+1. Global/source
    budgets run before per-account inserts, bounding key creation; expired rows
    are removed using the expiry index. A DB outage fails closed.
    """
    now = int(time.time())
    await db.execute(delete(AuthRateLimit).where(AuthRateLimit.expires_at <= now))
    if db.get_bind().dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    for scope, subject, limit, seconds in rules:
        start = now // seconds * seconds
        query = insert(AuthRateLimit).values(key=_key(scope, subject, seconds), window_start=start,
                                            count=1, expires_at=start + seconds)
        query = query.on_conflict_do_update(
            index_elements=[AuthRateLimit.key],
            set_={
                'count': case((AuthRateLimit.window_start != start, 1),
                              (AuthRateLimit.count <= limit, AuthRateLimit.count + 1),
                              else_=AuthRateLimit.count),
                'window_start': start, 'expires_at': start + seconds,
            },
        ).returning(AuthRateLimit.count)
        count = (await db.execute(query)).scalar_one()
        if count > limit:
            await db.commit()
            raise HTTPException(429, 'Too many authentication requests. Please try again later.',
                                headers={'Retry-After': str(max(1, start + seconds - now))})
    await db.commit()


async def account_limit(db: AsyncSession, email: str, action: str) -> None:
    subject = email.strip().lower()
    if action == 'delivery':
        rules = [('delivery', subject, 5, 900), ('delivery', subject, 20, 86400)]
    elif action == 'login':
        rules = [('login', subject, 10, 900)]
    else:
        # Shared across login OTP and password-reset verification, independent
        # of code record/purpose. Resending cannot restore this attempt budget.
        rules = [('verification', subject, 10, 900)]
    await consume(db, rules)


async def source_limit(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    # Use the ASGI peer only. Forwarded headers require trusted proxy handling
    # at Uvicorn; never read arbitrary X-Forwarded-For here.
    peer = request.client.host if request.client else 'unknown'
    try:
        ip = ipaddress.ip_address(peer)
        peer = str(ipaddress.ip_network(f'{ip}/64', strict=False)) if ip.version == 6 else str(ip)
    except ValueError:
        pass
    path = request.url.path.rstrip('/')
    if path.endswith(('/login', '/dev-login')):
        action, limit = 'login', 30
    elif path.endswith(('/verify-otp', '/otp/verify', '/reset-password')):
        action, limit = 'verify', 30
    else:
        action, limit = 'delivery', 20
    await consume(db, [('global', 'auth', 10000, 86400), ('source-' + action, peer, limit, 900)])
