import time
import pytest
import pytest_asyncio
from sqlalchemy import delete
from app.models.user import User
from app.models.audit import AuditLog
from test_account_roles import client, staff, auth
from app.modules.admin import health

@pytest_asyncio.fixture(autouse=True)
async def clean_admin(db_session):
    yield
    await db_session.rollback()
    await db_session.execute(delete(AuditLog))
    await db_session.execute(delete(User).where(User.role == "SUPER_ADMIN"))
    await db_session.commit()

@pytest.mark.asyncio
async def test_studio_suspension_restore_delete(client, db_session):
    admin, _, _ = await staff(db_session, "SUPER_ADMIN")
    owner, org, _ = await staff(db_session, "STUDIO")
    url = f"admin/organizations/{org.id}"
    assert (await client.patch(url+"/status", json={"status": "SUSPENDED"}, headers=auth(owner))).status_code == 403
    assert (await client.patch(url+"/status", json={"status": "SUSPENDED"}, headers=auth(admin))).status_code == 200
    assert (await client.get("auth/me", headers=auth(owner))).status_code == 403
    assert (await client.post("auth/login", json={"email": owner.email, "password": "strong-password"})).status_code == 403
    assert (await client.patch(url+"/status", json={"status": "ACTIVE"}, headers=auth(admin))).status_code == 200
    assert (await client.get("auth/me", headers=auth(owner))).status_code == 200
    assert (await client.delete(url, headers=auth(admin))).status_code == 200
    assert (await client.get("auth/me", headers=auth(owner))).status_code == 403
    assert (await client.get(url, headers=auth(admin))).status_code == 404
    assert (await client.patch(url+"/status", json={"status": "ACTIVE"}, headers=auth(admin))).status_code == 404

@pytest.mark.asyncio
async def test_people_roles_deletion_and_authorization(client, db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(health, "DB_PATH", tmp_path/"health.sqlite3")
    admin, _, _ = await staff(db_session, "SUPER_ADMIN")
    student, _, _ = await staff(db_session, "STUDENT")
    teacher, org, _ = await staff(db_session, "TEACHER")
    assert (await client.get("admin/people?role=STUDENT")).status_code == 401
    assert (await client.get("admin/people?role=STUDENT", headers=auth(student))).status_code == 403
    result = await client.get(f"admin/people?role=STUDENT&search={student.email}", headers=auth(admin))
    row = result.json()["items"][0]
    assert row["id"] == str(student.id)
    assert "password_hash" not in row and "attendance" not in row
    assert (await client.delete(f"admin/people/{admin.id}", headers=auth(admin))).status_code == 409
    assert (await client.get("admin/health", headers=auth(student))).status_code == 403
    assert (await client.get("admin/health?period=year", headers=auth(admin))).status_code == 422
    assert (await client.get("admin/health?tz=Invalid", headers=auth(admin))).status_code == 422
    assert (await client.get("admin/health", headers=auth(admin))).json()["latest"] is None
    # Last active teacher is protected.
    assert (await client.delete(f"admin/people/{teacher.id}", headers=auth(admin))).status_code == 409
    await staff(db_session, "TEACHER", org)
    assert (await client.delete(f"admin/people/{teacher.id}", headers=auth(admin))).status_code == 200
    assert (await client.get("auth/me", headers=auth(teacher))).status_code == 401
    assert (await client.delete(f"admin/people/{student.id}", headers=auth(admin))).status_code == 200
    assert (await client.get("auth/me", headers=auth(student))).status_code == 401
    assert (await client.get(f"admin/people?role=STUDENT&search={student.email}", headers=auth(admin))).json()["total_items"] == 0


def test_health_peaks_idle_gaps_and_worker_aggregation(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "DB_PATH", tmp_path/"metrics.sqlite3")
    now = int(time.time())//60*60
    with health.connection() as db:
        for ts, cpu in [(now-240,5),(now-180,8),(now-60,5),(now,95)]:
            for worker in (1,2):
                db.execute("INSERT INTO samples VALUES (?, ?, ?, 40, 30, 1, 2, 1, 20)",(ts, worker, cpu))
    report = health.report("month", "Asia/Kolkata")
    assert report["peak_stress"] == 95
    assert report["maximum_idle_seconds"] == 120
    assert report["requests"] == 16 and report["errors"] == 8
    assert report["samples"] == 4
    assert report["average_response_ms"] == 10
