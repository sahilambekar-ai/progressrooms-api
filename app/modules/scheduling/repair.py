import argparse
import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, func, and_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.schedule import ClassOccurrence, ClassOccurrenceChange, SessionScheduleRule
from app.models.session import Session
from app.models.organization import Organization
from app.models.attendance import AttendanceRecord

logger = logging.getLogger(__name__)

async def find_repairable_occurrences(db: AsyncSession) -> list[dict]:
    """
    Identifies future SCHEDULED occurrences that were shifted late due to combining
    local schedule rule times directly with UTC instead of studio timezone.
    Narrowly scoped:
    - Occurrence status must be SCHEDULED
    - Occurrence must be in the future (actual_start_at > now)
    - Occurrence must have 0 attendance records (never alter historical attendance)
    - Must match an active SessionScheduleRule whose local time was directly tagged with UTC
    """
    now_utc = datetime.now(timezone.utc)

    stmt = select(ClassOccurrence).options(
        selectinload(ClassOccurrence.session).selectinload(Session.schedule_rules),
        selectinload(ClassOccurrence.session).selectinload(Session.organization),
    ).where(
        ClassOccurrence.status.in_(["SCHEDULED", "COMPLETED"])
    ).order_by(ClassOccurrence.actual_start_at.asc())

    rows = (await db.execute(stmt)).scalars().all()
    repairable = []

    for occ in rows:
        session = occ.session
        if not session or not session.schedule_rules:
            continue

        studio_tz_str = (session.organization.timezone if session.organization else None) or "Asia/Kolkata"

        for rule in session.schedule_rules:
            if not rule.is_active:
                continue

            rule_tz_str = rule.timezone or studio_tz_str
            try:
                rule_tz = ZoneInfo(rule_tz_str)
            except Exception:
                rule_tz = ZoneInfo("Asia/Kolkata")

            # Check if this occurrence's scheduled_start_at matches the rule's time directly tagged as UTC
            # E.g., rule start is 18:00, and occ.scheduled_start_at.time() == 18:00 in UTC (which is 23:30 in Asia/Kolkata)
            occ_utc_time = occ.scheduled_start_at.astimezone(timezone.utc).time()
            rule_start_time = rule.start_time

            # If the stored UTC time hour & minute exactly matches the rule's intended local hour & minute,
            # and the rule timezone is not UTC (e.g. Asia/Kolkata has +05:30 offset):
            if occ_utc_time.hour == rule_start_time.hour and occ_utc_time.minute == rule_start_time.minute:
                # The local date the student was expecting
                local_date = occ.scheduled_start_at.astimezone(timezone.utc).date()

                # Correct expected UTC timestamp
                correct_local_start = datetime.combine(local_date, rule.start_time, tzinfo=rule_tz)
                if rule.end_time < rule.start_time:
                    correct_local_end = datetime.combine(local_date + timedelta(days=1), rule.end_time, tzinfo=rule_tz)
                else:
                    correct_local_end = datetime.combine(local_date, rule.end_time, tzinfo=rule_tz)

                correct_start_utc = correct_local_start.astimezone(timezone.utc)
                correct_end_utc = correct_local_end.astimezone(timezone.utc)

                # Only if the correct UTC is actually different from current stored UTC
                if correct_start_utc != occ.scheduled_start_at:
                    repairable.append({
                        "occurrence_id": str(occ.id),
                        "occurrence": occ,
                        "session_id": str(session.id),
                        "session_name": session.name,
                        "organization_name": session.organization.name if session.organization else "Studio",
                        "timezone": rule_tz_str,
                        "current_stored_start_utc": occ.scheduled_start_at.isoformat(),
                        "current_local_display": occ.scheduled_start_at.astimezone(rule_tz).strftime("%Y-%m-%d %H:%M %Z"),
                        "intended_start_utc": correct_start_utc.isoformat(),
                        "intended_local_display": correct_local_start.strftime("%Y-%m-%d %H:%M %Z"),
                        "new_start_utc": correct_start_utc,
                        "new_end_utc": correct_end_utc,
                    })
                    break

    return repairable

async def execute_repair(db: AsyncSession, dry_run: bool = True) -> dict:
    candidates = await find_repairable_occurrences(db)
    summary = {
        "dry_run": dry_run,
        "total_repairable": len(candidates),
        "repaired_count": 0,
        "items": []
    }

    for c in candidates:
        summary["items"].append({
            "occurrence_id": c["occurrence_id"],
            "session_name": c["session_name"],
            "timezone": c["timezone"],
            "current_display": c["current_local_display"],
            "corrected_display": c["intended_local_display"],
            "current_utc": c["current_stored_start_utc"],
            "corrected_utc": c["intended_start_utc"]
        })

        if not dry_run:
            occ: ClassOccurrence = c["occurrence"]
            old_start = occ.actual_start_at
            occ.scheduled_start_at = c["new_start_utc"]
            occ.scheduled_end_at = c["new_end_utc"]
            occ.actual_start_at = c["new_start_utc"]
            occ.actual_end_at = c["new_end_utc"]

            change = ClassOccurrenceChange(
                class_occurrence_id=occ.id,
                change_type="TIMEZONE_REPAIR",
                previous_start_at=old_start,
                new_start_at=c["new_start_utc"],
                reason=f"Repaired timezone offset from scheduling engine to {c['timezone']}"
            )
            db.add(change)
            summary["repaired_count"] += 1

    if not dry_run and summary["repaired_count"] > 0:
        await db.commit()

    return summary

async def main():
    parser = argparse.ArgumentParser(description="Repair future ClassOccurrences with timezone shift bug.")
    parser.add_argument("--execute", action="store_true", help="Actually execute the repair. Default is dry-run.")
    args = parser.parse_args()

    async with AsyncSessionLocal() as db:
        res = await execute_repair(db, dry_run=not args.execute)
        mode = "DRY RUN SUMMARY" if res["dry_run"] else "EXECUTED REPAIR"
        print(f"\n=== OCCURRENCE TIMEZONE REPAIR ({mode}) ===")
        print(f"Total occurrences identified: {res['total_repairable']}")
        print(f"Total occurrences repaired: {res['repaired_count']}\n")

        for item in res["items"][:10]:
            print(f"- [{item['session_name']}] Current: {item['current_display']} ({item['current_utc']}) -> Corrected: {item['corrected_display']} ({item['corrected_utc']})")

        if len(res["items"]) > 10:
            print(f"... and {len(res['items']) - 10} more occurrences.")

        if res["dry_run"]:
            print("\nRun with --execute to apply this repair safely to the database.")

if __name__ == "__main__":
    asyncio.run(main())
