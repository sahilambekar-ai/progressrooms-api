from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.modules.attendance.status import calendar_status
from app.modules.attendance.calendar import event_dict

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("offset,occurrence,record,expected", [
    (-86400, "SCHEDULED", None, "PRESENT"),
    (-60, "SCHEDULED", None, "PRESENT"),
    (0, "SCHEDULED", None, "PRESENT"),
    (-60, "IN_PROGRESS", None, "PRESENT"),
    (-86400, "COMPLETED", None, "PRESENT"),
    (-86400, "COMPLETED", "UNMARKED", "PRESENT"),
    (60, "SCHEDULED", None, "SCHEDULED"),
    (86400, "RESCHEDULED", None, "SCHEDULED"),
    (-86400, "SCHEDULED", "ABSENT", "ABSENT"),
    (-86400, "COMPLETED", "EXCUSED", "EXCUSED"),
    (-86400, "COMPLETED", "CANCELLED", "EXCUSED"),
    (60, "CANCELLED", None, "EXCUSED"),
    (-60, "CANCELLED", None, "EXCUSED"),
    (60, "SCHEDULED", "ATTENDED", "PRESENT"),
])
def test_attendance_defaults_and_explicit_marks(offset, occurrence, record, expected):
    assert calendar_status(NOW + timedelta(seconds=offset), occurrence, record, NOW) == expected


def test_start_time_is_compared_as_an_instant_across_timezones():
    start = NOW.astimezone(ZoneInfo("Asia/Kolkata")) - timedelta(minutes=1)
    assert calendar_status(start, "SCHEDULED", now=NOW) == "PRESENT"
    assert calendar_status(NOW.replace(tzinfo=None), "SCHEDULED", now=NOW) == "PRESENT"


def test_calendar_returns_start_instant_for_client_status_transition():
    start = datetime.now(timezone.utc) + timedelta(minutes=5)
    occurrence = SimpleNamespace(id="class", actual_start_at=start, status="SCHEDULED")
    event = event_dict(occurrence, SimpleNamespace(name="Yoga", full_description="Guided yoga practice"), None, None, ZoneInfo("Asia/Kolkata"))
    assert event["description"] == "Guided yoga practice"
    assert event["status"] == "SCHEDULED"
    assert datetime.fromisoformat(event["startAt"]) == start
