"""Bounded synthetic ASGI/DB resource soak; no live providers or existing tables.

Requires APP_ENV=test and DATABASE_REVIEW_POSTGRES_URL for a disposable PostgreSQL
DB. Owns one random schema; records warm-up and six batches of 500 requests.
"""

import asyncio
import gc
import json
import os
import statistics
import subprocess
import threading
import time
import tracemalloc
import uuid
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import get_db
from app.core.security import create_access_token
from app.main import api_v1, app
from app.models import Base
from app.models.organization import Organization
from app.models.plan import Plan
from app.models.session import ClassType, Session, SessionPricing
from app.models.user import User


async def main():
    url = os.environ["DATABASE_REVIEW_POSTGRES_URL"].replace(
        "postgresql:", "postgresql+asyncpg:"
    )
    schema = "runtime_" + uuid.uuid4().hex
    root = create_async_engine(url)
    async with root.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url,
        pool_size=5,
        max_overflow=5,
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    previous = dict(app.dependency_overrides)
    child_previous = dict(api_v1.dependency_overrides)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            org = Organization(name="Runtime Studio", slug="runtime-studio")
            user = User(
                email="runtime@example.test",
                full_name="Runtime",
                role="STUDENT",
                is_verified=True,
            )
            db.add_all([org, user, Plan(name="Starter", slug="starter", price=0)])
            await db.flush()
            category = ClassType(organization_id=org.id, name="Yoga", slug="yoga")
            db.add(category)
            await db.flush()
            for i in range(100):
                course = Session(
                    organization_id=org.id,
                    class_type_id=category.id,
                    name=f"Class {i}",
                    slug=f"class-{i}",
                )
                db.add(course)
                await db.flush()
                db.add(
                    SessionPricing(
                        session_id=course.id,
                        pricing_type="FREE",
                        price=0,
                        currency="INR",
                    )
                )
            await db.commit()
            token = create_access_token({"sub": str(user.id), "sv": 0})

        async def database():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db] = database
        api_v1.dependency_overrides[get_db] = database
        latencies = []
        statuses = {}
        lag = []
        stop = False

        async def heartbeat():
            while not stop:
                before = time.perf_counter()
                await asyncio.sleep(0.02)
                lag.append(max(0, time.perf_counter() - before - 0.02) * 1000)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://audit",
            headers={"Authorization": "Bearer " + token},
        ) as client:
            paths = [
                "/health",
                "/api/v1/plans",
                "/api/v1/auth/me",
                "/api/v1/public/organizations/runtime-studio",
            ]
            semaphore = asyncio.Semaphore(10)

            async def one(i, record=True):
                async with semaphore:
                    started = time.perf_counter()
                    response = await client.get(paths[i % len(paths)])
                    duration = (time.perf_counter() - started) * 1000
                    if record:
                        latencies.append(duration)
                        statuses[str(response.status_code)] = (
                            statuses.get(str(response.status_code), 0) + 1
                        )
                    assert response.status_code == 200, (
                        paths[i % 4],
                        response.status_code,
                        response.text[:100],
                    )

            await asyncio.gather(*(one(i, False) for i in range(200)))
            traced = os.environ.get("PROFILE_TRACE_FRAMES", "10") != "0"
            if traced:
                tracemalloc.start(10)
            gc.collect()
            start = tracemalloc.take_snapshot() if traced else None
            samples = []
            task = asyncio.create_task(heartbeat())
            started = time.perf_counter()
            cpu = time.process_time()
            for batch in range(6):
                await asyncio.gather(*(one(i) for i in range(500)))
                gc.collect()
                current, peak = (
                    tracemalloc.get_traced_memory() if traced else (None, None)
                )
                rss = (
                    int(
                        subprocess.check_output(
                            ["ps", "-o", "rss=", "-p", str(os.getpid())]
                        ).strip()
                    )
                    * 1024
                )
                samples.append(
                    {
                        "batch": batch + 1,
                        "python_bytes": current,
                        "python_peak_bytes": peak,
                        "rss_bytes": rss,
                        "tasks": len(asyncio.all_tasks()),
                        "threads": threading.active_count(),
                        "checked_out_connections": engine.pool.checkedout(),
                    }
                )
            elapsed = time.perf_counter() - started
            cpu_used = time.process_time() - cpu
            stop = True
            await task
            gc.collect()
            end = tracemalloc.take_snapshot() if traced else None
            growth = [
                {
                    "location": str(s.traceback),
                    "bytes": s.size_diff,
                    "allocations": s.count_diff,
                }
                for s in (end.compare_to(start, "lineno")[:15] if traced else [])
            ]
            if traced:
                tracemalloc.stop()

            def percentile(values, p):
                return sorted(values)[min(len(values) - 1, int(len(values) * p))]

            result = {
                "mode": "in-process ASGI + real PostgreSQL; trace=" + str(traced),
                "requests": len(latencies),
                "concurrency": 10,
                "seconds": elapsed,
                "cpu_seconds": cpu_used,
                "requests_per_second": len(latencies) / elapsed,
                "statuses": statuses,
                "latency_ms": {
                    "p50": statistics.median(latencies),
                    "p95": percentile(latencies, 0.95),
                    "p99": percentile(latencies, 0.99),
                },
                "event_loop_lag_ms": {"p95": percentile(lag, 0.95), "max": max(lag)},
                "samples": samples,
                "top_retained_growth": growth,
            }
            Path(
                os.environ.get("PROFILE_OUTPUT", "/private/tmp/item7-runtime.json")
            ).write_text(json.dumps(result, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in result.items()
                        if k not in {"samples", "top_retained_growth"}
                    },
                    indent=2,
                )
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        api_v1.dependency_overrides.clear()
        api_v1.dependency_overrides.update(child_previous)
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await root.dispose()


if __name__ == "__main__":
    asyncio.run(main())
