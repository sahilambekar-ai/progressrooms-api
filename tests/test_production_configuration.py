"""Shared-environment startup validation and runtime defense in depth."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from app.core.config import Settings, settings
from app.modules.commerce.service import mock_payments
from test_full_audit_auth import audit_client, student  # noqa: F401

STRONG_TEST_KEY = 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0'


def shared(**changes):
    return Settings(_env_file=None, **{
        'APP_ENV': 'production', 'SECRET_KEY': STRONG_TEST_KEY,
        'WEBSITE_PUBLIC_ORIGIN': 'https://app.example.com',
        'BACKEND_CORS_ORIGINS': ['https://app.example.com'],
        'AUTH_SHOW_OTP_PREVIEW': False, 'DEV_TEST_LOGIN_ENABLED': False,
        'ZOOM_PROVIDER_MODE': 'disabled', **changes})


@pytest.mark.parametrize('env', ['production', 'prod', 'uat', 'staging', 'stage', ' Production '])
def test_shared_environments_accept_explicit_safe_configuration(env):
    value = shared(APP_ENV=env)
    assert not value.is_local_environment
    assert not value.otp_preview_enabled and not value.development_login_enabled


@pytest.mark.parametrize('key', [Settings.model_fields['SECRET_KEY'].default, '', 'short',
    'x' * 128, 'GENERATE_A_UNIQUE_RANDOM_SECRET', 'your-secret-key-needs-to-change-before-production',
    'ci-only-auth-signing-key-7e19b6a4302d8f5c'])
def test_shared_environments_reject_weak_default_and_placeholder_keys(key):
    with pytest.raises(ValidationError):
        shared(SECRET_KEY=key)


@pytest.mark.parametrize('env', ['production', 'uat', 'staging'])
@pytest.mark.parametrize('flag', ['AUTH_SHOW_OTP_PREVIEW', 'DEV_TEST_LOGIN_ENABLED'])
def test_shared_environments_reject_developer_auth_flags(env, flag):
    with pytest.raises(ValidationError, match='forbidden'):
        shared(APP_ENV=env, **{flag: True})


@pytest.mark.parametrize('env', ['development', 'dev', 'local', 'test', 'testing'])
def test_local_modes_explicitly_support_isolated_developer_workflows(env):
    local = Settings(_env_file=None, APP_ENV=env, SECRET_KEY='local-only',
                     AUTH_SHOW_OTP_PREVIEW=True, DEV_TEST_LOGIN_ENABLED=True,
                     ZOOM_PROVIDER_MODE='mock')
    assert local.is_local_environment and local.otp_preview_enabled and local.development_login_enabled


@pytest.mark.parametrize('env', ['prodution', 'unknown', '', 'uat-local'])
def test_unknown_environment_names_cannot_bypass_guards(env):
    with pytest.raises(ValidationError):
        shared(APP_ENV=env)


@pytest.mark.parametrize('field,value', [
    ('WEBSITE_PUBLIC_ORIGIN', 'http://app.example.com'),
    ('WEBSITE_PUBLIC_ORIGIN', 'https://localhost'),
    ('WEBSITE_PUBLIC_ORIGIN', 'https://127.0.0.1'),
    ('WEBSITE_PUBLIC_ORIGIN', 'https://user:secret@app.example.com'),
    ('WEBSITE_PUBLIC_ORIGIN', 'https://app.example.com/path'),
    ('WEBSITE_PUBLIC_ORIGIN', 'https://app.example.com?target=evil'),
    ('WEBSITE_PUBLIC_ORIGIN', 'https://app.example.com/#fragment'),
    ('BACKEND_CORS_ORIGINS', ['*']),
    ('BACKEND_CORS_ORIGINS', []),
    ('BACKEND_CORS_ORIGINS', ['http://localhost:3000']),
    ('BACKEND_CORS_ORIGINS', ['https://app.example.com/extra']),
    ('ZOOM_PROVIDER_MODE', 'mock'),
    ('ZOOM_PROVIDER_MODE', 'typo'),
    ('ALGORITHM', 'none'),
])
def test_shared_configuration_rejects_unsafe_origins_or_modes(field, value):
    with pytest.raises(ValidationError):
        shared(**{field: value})


@pytest.mark.parametrize('algorithm,minimum', [('HS256', 32), ('HS384', 48), ('HS512', 64)])
def test_signing_key_length_matches_hmac_algorithm(algorithm, minimum):
    key = (STRONG_TEST_KEY * 2)[:minimum]
    assert shared(ALGORITHM=algorithm, SECRET_KEY=key)
    with pytest.raises(ValidationError):
        shared(ALGORITHM=algorithm, SECRET_KEY=key[:-1])


def test_configuration_errors_hide_input_secrets():
    with pytest.raises(ValidationError) as caught:
        shared(SECRET_KEY=STRONG_TEST_KEY, AUTH_SHOW_OTP_PREVIEW=True)
    assert STRONG_TEST_KEY not in str(caught.value)


def test_actual_app_import_fails_closed_without_shared_configuration(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]))
    for field in Settings.model_fields:
        env.pop(field, None)
    result = subprocess.run([sys.executable, '-c', 'from app.main import app'], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert 'SECRET_KEY' in result.stderr
    assert Settings.model_fields['SECRET_KEY'].default not in result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize('env', ['production', 'uat', 'staging'])
async def test_runtime_flags_cannot_enable_shared_dev_login_or_mock_payments(audit_client, monkeypatch, env):
    monkeypatch.setattr(settings, 'APP_ENV', env)
    monkeypatch.setattr(settings, 'DEV_TEST_LOGIN_ENABLED', True)
    response = await audit_client.post('auth/dev-login', json={'role': 'STUDENT'})
    assert response.status_code == 404
    assert not mock_payments()


@pytest.mark.asyncio
async def test_runtime_shared_environment_never_returns_otp_previews(audit_client, db_session, monkeypatch):
    user = await student(db_session)
    monkeypatch.setattr(settings, 'APP_ENV', 'production')
    monkeypatch.setattr(settings, 'AUTH_SHOW_OTP_PREVIEW', True)
    for path in ['auth/request-otp', 'auth/forgot-password']:
        response = await audit_client.post(path, json={'email': user.email})
        assert response.status_code == 200
        assert response.json()['otp_preview'] is None
