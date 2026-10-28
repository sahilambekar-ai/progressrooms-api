"""Verify owned telemetry connections close on successful and failed operations."""
import sqlite3

import pytest

from app.modules.admin import health


@pytest.mark.parametrize("operation", ["save", "report", "failure"])
def test_telemetry_closes_sqlite_connections(tmp_path, monkeypatch, operation):
    opened = []
    connect = sqlite3.connect

    def track(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(health, "DB_PATH", tmp_path / "health.sqlite3")
    monkeypatch.setattr(health.sqlite3, "connect", track)
    monkeypatch.setattr(health, "host_sample", lambda: (10, 20, 30, 1))
    if operation == "save":
        health.save_sample((1, 0, 5.0))
    elif operation == "report":
        assert health.report("day", "UTC")["samples"] == 0
    else:
        with pytest.raises(sqlite3.ProgrammingError):
            health.save_sample((1,))  # Wrong number of bindings forces rollback.
    assert len(opened) == 1
    try:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            opened[0].execute("SELECT 1")
    finally:
        for connection in opened:
            connection.close()
