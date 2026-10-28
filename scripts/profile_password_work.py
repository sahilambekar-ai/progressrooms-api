"""Compare event-loop lag for eight synthetic password verifications."""

import asyncio
import json
import time

from app.core.password_work import check_password
from app.core.security import get_password_hash, verify_password


async def measure(threaded, encoded):
    lag = []
    done = False

    async def monitor():
        while not done:
            before = time.perf_counter()
            await asyncio.sleep(0.005)
            lag.append(max(0, time.perf_counter() - before - 0.005) * 1000)

    task = asyncio.create_task(monitor())
    await asyncio.sleep(0)

    async def verify():
        if threaded:
            return await check_password("synthetic-password", encoded)
        return verify_password("synthetic-password", encoded)

    start = time.perf_counter()
    assert all(await asyncio.gather(*(verify() for _ in range(8))))
    elapsed = time.perf_counter() - start
    done = True
    await task
    return {"seconds": elapsed, "max_loop_lag_ms": max(lag)}


async def main():
    encoded = get_password_hash("synthetic-password")
    result = {}
    for threaded in [False, True]:
        result["bounded_threads" if threaded else "blocking_baseline"] = await measure(
            threaded, encoded
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
