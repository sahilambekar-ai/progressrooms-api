"""Bounded, minute-resolution telemetry. No request bodies, identities or tokens stored."""
import asyncio
import logging
import os
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import Depends, Query
from app.core.config import settings
from app.modules.auth.dependencies import require_superadmin
from app.modules.admin.router import router

log = logging.getLogger(__name__)
DB_PATH = Path(os.getenv("PLATFORM_METRICS_PATH", "data/platform-health.sqlite3"))
PROC = Path(os.getenv("PLATFORM_HOST_PROC", "/proc"))
counts = [0, 0, 0.0]  # requests, 5xx, cumulative response milliseconds
previous_cpu = None

def connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=5)
    db.execute("PRAGMA cache_size=-512")
    db.execute("CREATE TABLE IF NOT EXISTS samples (ts INTEGER, worker INTEGER, cpu REAL, memory REAL, disk REAL, load REAL, requests INTEGER, errors INTEGER, latency REAL, PRIMARY KEY(ts, worker))")
    return db

def host_sample():
    global previous_cpu
    cpu = memory = None
    try:
        values = list(map(int, (PROC / "stat").read_text().splitlines()[0].split()[1:9]))
        total, idle = sum(values), values[3]
        if previous_cpu and total > previous_cpu[0]:
            cpu = round(max(0, min(100, 100 * (1-(idle-previous_cpu[1])/(total-previous_cpu[0])))), 2)
        previous_cpu = (total, idle)
        mem = {line.split(':')[0]: int(line.split()[1]) for line in (PROC / "meminfo").read_text().splitlines() if ':' in line and len(line.split()) > 1}
        memory = round(100 * (1-mem['MemAvailable']/mem['MemTotal']), 2)
    except (OSError, ValueError, KeyError, IndexError):
        pass
    disk = shutil.disk_usage(os.getenv("PLATFORM_DISK_PATH", "/"))
    return cpu, memory, round(100*disk.used/disk.total, 2), os.getloadavg()[0]

def save_sample(snapshot):
    with closing(connection()) as db, db:
        db.execute("INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(time.time())//60*60, os.getpid(), *host_sample(), *snapshot))
        db.execute("DELETE FROM samples WHERE ts < ?", (int(time.time())-32*86400,))

async def collect():
    while True:
        try:
            snapshot = tuple(counts)
            counts[:] = [0, 0, 0.0]
            await asyncio.to_thread(save_sample, snapshot)
        except Exception:
            log.exception("Platform telemetry sampling failed")
        await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(collect())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        from app.core.database import engine
        await engine.dispose()

def report(period, tz):
    now = datetime.now(ZoneInfo(tz))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start -= timedelta(days=start.weekday())
    elif period == "month":
        start = start.replace(day=1)
    with closing(connection()) as db, db:
        rows = db.execute("SELECT ts, MAX(cpu), MAX(memory), MAX(disk), MAX(load), SUM(requests), SUM(errors), SUM(latency) FROM samples WHERE ts >= ? GROUP BY ts ORDER BY ts", (start.timestamp(),)).fetchall()
    # Keep peaks when reducing chart data; gaps stay visible as separate time buckets.
    width = {"day": 300, "week": 1800, "month": 7200}[period]
    buckets = {}
    peak = None
    longest = run = 0
    last = None
    for ts, cpu, mem, disk, load, requests, errors, latency in rows:
        stress = max(cpu, mem) if cpu is not None and mem is not None else None
        if stress is not None:
            peak = max(peak or 0, stress)
        # Idle is sampled CPU <= 10%; never infer idle across missing samples.
        run = (run+60 if last is not None and ts-last <= 90 else 60) if cpu is not None and cpu <= 10 else 0
        longest = max(longest, run)
        last = ts
        key = ts//width*width
        bucket = buckets.setdefault(key, {"time": key, "stress": None, "cpu": None, "memory": None, "requests": 0, "errors": 0})
        for field, value in (("stress", stress), ("cpu", cpu), ("memory", mem)):
            if value is not None:
                bucket[field] = max(bucket[field] or 0, value)
        bucket["requests"] += requests
        bucket["errors"] += errors
    latest = rows[-1] if rows else None
    return dict(period=period, timezone=tz, environment=settings.APP_ENV, peak_stress=peak,
        period_start=int(start.timestamp()), period_end=int(now.timestamp()), bucket_seconds=width,
        maximum_idle_seconds=longest, samples=len(rows), points=list(buckets.values()),
        first_sample=rows[0][0] if rows else None,
        latest=None if not latest else dict(time=latest[0], cpu=latest[1], memory=latest[2], disk=latest[3], load=latest[4]),
        requests=sum(r[5] for r in rows), errors=sum(r[6] for r in rows),
        average_response_ms=round(sum(r[7] for r in rows)/sum(r[5] for r in rows), 1) if sum(r[5] for r in rows) else None)

@router.get("/health")
async def platform_health(period: str = Query("day", pattern="^(day|week|month)$"),
    tz: str = "Asia/Kolkata", admin=Depends(require_superadmin)):
    from fastapi import HTTPException
    try:
        ZoneInfo(tz)
    except (KeyError, ValueError):
        raise HTTPException(422, "Invalid timezone")
    return await asyncio.to_thread(report, period, tz)
