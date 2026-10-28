"""Bound expensive password work off the event loop (four operations per worker)."""

from anyio import CapacityLimiter
from anyio.lowlevel import RunVar
from anyio.to_thread import run_sync

from app.core.security import get_password_hash, verify_password

_limiter = RunVar("password_work_limiter")


async def run(function, *args):
    try:
        limiter = _limiter.get()
    except LookupError:
        limiter = CapacityLimiter(4)
        _limiter.set(limiter)
    # Cancellation waits for owned CPU work; it never abandons an unbounded thread.
    return await run_sync(function, *args, limiter=limiter)


async def hash_password(value):
    return await run(get_password_hash, value)


async def check_password(value, encoded):
    return await run(verify_password, value, encoded)
