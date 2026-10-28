import asyncio
import logging
import threading
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from test_account_roles import auth, client, staff  # noqa: F401

from app.core.config import settings
from app.core.financial_data import decrypt_account, encrypt_account, mask_account
from app.core.observability import request_metrics, unhandled_error
from app.models.audit import AuditLog
from app.models.organization import OrganizationSetting, OrganizationStudent


def test_bank_ciphertext_is_versioned_masked_and_independent(monkeypatch):
    monkeypatch.setattr(
        settings, "FINANCIAL_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    value = encrypt_account("1234567890")
    assert len(encrypt_account("A" * 100)) <= 255
    assert value.startswith("fin1:") and "1234567890" not in value
    assert decrypt_account(value) == "1234567890"
    assert mask_account(value) == "••••7890"
    assert mask_account("123") == "••••"
    monkeypatch.setattr(settings, "SECRET_KEY", "a-different-auth-signing-key")
    assert decrypt_account(value) == "1234567890"
    monkeypatch.setattr(
        settings, "FINANCIAL_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    assert mask_account(value) == "•••• (key recovery required)"
    with pytest.raises(HTTPException):
        decrypt_account(value)


def test_shared_bank_writes_fail_closed_without_key(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "FINANCIAL_DATA_ENCRYPTION_KEY", "")
    with pytest.raises(HTTPException) as error:
        encrypt_account("1234567890")
    assert error.value.status_code == 503
    assert mask_account("1234567890") == "••••7890"


@pytest.mark.asyncio
async def test_legacy_bank_migration_dry_run_apply_and_repeat(db_session, monkeypatch):
    from scripts.encrypt_bank_accounts import migrate

    _, org, _ = await staff(db_session, "STUDIO")
    row = OrganizationSetting(organization_id=org.id, account_number_enc="1234567890")
    db_session.add(row)
    await db_session.commit()
    monkeypatch.setattr(
        settings, "FINANCIAL_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    result = await migrate(db_session)
    assert result["plaintext_rows"] == 1 and row.account_number_enc == "1234567890"
    await migrate(db_session, True)
    assert (
        row.account_number_enc.startswith("fin1:")
        and decrypt_account(row.account_number_enc) == "1234567890"
    )
    result = await migrate(db_session, True)
    assert result["encrypted_rows"] == 1 and result["plaintext_rows"] == 0


@pytest.mark.asyncio
async def test_student_profile_changes_require_consent_and_audit_only_field_names(
    client, db_session
):
    owner, org, _ = await staff(db_session, "STUDIO")
    student, _, _ = await staff(db_session, "STUDENT")
    student.phone = "+919876543210"
    record = OrganizationStudent(organization_id=org.id, user_id=student.id)
    db_session.add(record)
    await db_session.commit()
    path = f"/students/{record.id}"
    body = {"full_name": "Changed private name", "phone": student.phone}
    denied = await client.patch(path, headers=auth(owner), json=body)
    assert denied.status_code == 422
    await db_session.refresh(student)
    assert student.full_name != "Changed private name"
    allowed = await client.patch(
        path,
        headers=auth(owner),
        json={
            **body,
            "profile_consent_confirmed": True,
            "profile_consent_reference": "REQUEST-001",
        },
    )
    assert allowed.status_code == 200, allowed.text
    event = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == student.id,
            AuditLog.action == "PROFILE_CONSENT_ATTESTED",
        )
    )
    assert event.new_data == {
        "fields": ["full_name"],
        "consent_reference": "REQUEST-001",
    }
    assert "Changed private name" not in str(event.new_data)
    # Unchanged personal data + local studio notes require no new attestation.
    assert (
        await client.patch(
            path,
            headers=auth(owner),
            json={"phone": student.phone, "student_notes": "Local note"},
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_teacher_personal_update_requires_consent_but_status_only_does_not(
    client, db_session
):
    from app.core.security import get_password_hash
    from app.models.organization import OrganizationMember
    from app.models.user import User

    owner, org, _ = await staff(db_session, "STUDIO")
    owner.password_hash = get_password_hash("owner-password")
    teacher = User(
        email="consent-teacher@example.test",
        full_name="Teacher",
        role="TEACHER",
        is_verified=True,
    )
    db_session.add(teacher)
    await db_session.flush()
    member = OrganizationMember(
        organization_id=org.id,
        user_id=teacher.id,
        role="TEACHER",
        teacher_code="87654321",
        can_teach=True,
    )
    db_session.add(member)
    await db_session.commit()
    path = f"/teachers/{member.id}"
    body = {"studio_password": "owner-password", "name": "Corrected Teacher"}
    assert (await client.put(path, headers=auth(owner), json=body)).status_code == 422
    assert (
        await client.put(
            path,
            headers=auth(owner),
            json={
                **body,
                "profile_consent_confirmed": True,
                "profile_consent_reference": "REQUEST-002",
            },
        )
    ).status_code == 200
    assert (
        await client.put(
            path,
            headers=auth(owner),
            json={"studio_password": "owner-password", "is_active": True},
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_request_observability_omits_sensitive_values(caplog):
    app = FastAPI()
    app.middleware("http")(request_metrics)
    app.add_exception_handler(Exception, unhandled_error)

    @app.get("/objects/{object_id}")
    async def broken(object_id: str):
        raise RuntimeError("secret-error-value")

    with caplog.at_level(logging.INFO, logger="progressrooms.requests"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
            base_url="http://test",
        ) as http:
            response = await http.get(
                "/objects/private-identity?code=private-code",
                headers={"X-Request-ID": "attacker-controlled"},
            )
    assert (
        response.status_code == 500
        and response.headers["X-Request-ID"] != "attacker-controlled"
    )
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "/objects/{object_id}" in caplog.text
    for value in [
        "secret-error-value",
        "private-identity",
        "private-code",
        "attacker-controlled",
    ]:
        assert value not in caplog.text and value not in response.text


@pytest.mark.asyncio
async def test_readiness_fails_without_leaking_database_errors(client, monkeypatch):
    from app.core.database import get_db
    from app.main import app

    previous = app.dependency_overrides.get(get_db)

    async def failing():
        yield type(
            "DB",
            (),
            {
                "execute": AsyncMock(
                    side_effect=RuntimeError("private-database-address")
                )
            },
        )()

    app.dependency_overrides[get_db] = failing
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.get("/ready")
            assert response.status_code == 503 and response.json() == {
                "status": "unavailable"
            }
    finally:
        if previous:
            app.dependency_overrides[get_db] = previous
        else:
            app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_password_threads_are_bounded_and_do_not_block_loop():
    from app.core.password_work import run

    lock = threading.Lock()
    active = peak = 0
    loop_thread = threading.get_ident()
    worker_threads = set()

    def work():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            worker_threads.add(threading.get_ident())
        time.sleep(0.02)
        with lock:
            active -= 1

    await asyncio.gather(*(run(work) for _ in range(12)))
    assert 1 < peak <= 4 and loop_thread not in worker_threads


@pytest.mark.asyncio
async def test_telemetry_task_closes_even_when_lifespan_body_fails(monkeypatch):
    from app.modules.admin import health

    cancelled = asyncio.Event()

    async def collect():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(health, "collect", collect)
    with pytest.raises(RuntimeError):
        async with health.lifespan(None):
            await asyncio.sleep(0)
            raise RuntimeError("shutdown failure")
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name", ["app.modules.communication.worker", "app.modules.zoom.maintenance"]
)
async def test_workers_dispose_connections_when_stopping(monkeypatch, module_name):
    import importlib
    from types import SimpleNamespace

    from app.core import database

    worker = importlib.import_module(module_name)
    dispose = AsyncMock()
    monkeypatch.setattr(database, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(worker, "_run", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await worker.main()
    dispose.assert_awaited_once()


def test_shared_runtime_disables_parent_and_mounted_docs():
    import json
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "APP_ENV": "production",
        "SECRET_KEY": "7e19b6a4302d8f5c4b87e3d196ab2054d6013fab7cd95e602a4f8931bcdd765a",
        "WEBSITE_PUBLIC_ORIGIN": "https://example.test",
        "BACKEND_CORS_ORIGINS": '["https://example.test"]',
        "DEV_TEST_LOGIN_ENABLED": "false",
        "AUTH_SHOW_OTP_PREVIEW": "false",
        "ZOOM_PROVIDER_MODE": "disabled",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from app.main import app,api_v1; print(json.dumps([app.docs_url,app.redoc_url,app.openapi_url,api_v1.docs_url,api_v1.redoc_url,api_v1.openapi_url]))",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [None] * 6


@pytest.mark.parametrize("value", ["", "A" * 101, "雪" * 60, "12\n34"])
def test_bank_values_fit_the_encrypted_storage_contract(value):
    with pytest.raises(HTTPException) as error:
        encrypt_account(value)
    assert error.value.status_code == 422
