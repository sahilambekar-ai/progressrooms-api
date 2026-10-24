"""Resolve attendance consistently using the occurrence's actual start instant."""
from datetime import datetime, timezone


def calendar_status(start_at, occurrence_status, recorded_status=None, now=None):
    start = start_at.replace(tzinfo=timezone.utc) if start_at.tzinfo is None else start_at
    current = now or datetime.now(timezone.utc)
    if recorded_status == "ABSENT":
        return "ABSENT"
    if recorded_status in {"EXCUSED", "CANCELLED"} or occurrence_status == "CANCELLED":
        return "EXCUSED"
    if recorded_status in {"PRESENT", "ATTENDED"}:
        return "PRESENT"
    if start > current:
        return "SCHEDULED"
    return "PRESENT"


def roster_status(start_at, occurrence_status, recorded_status=None):
    status = calendar_status(start_at, occurrence_status, recorded_status)
    return "ATTENDED" if status == "PRESENT" else status
