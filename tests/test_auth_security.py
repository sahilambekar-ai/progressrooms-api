"""Password migration, revocation and abuse controls at the API boundary."""
import uuid
from unittest.mock import AsyncMock

import bcrypt
import jwt
import pytest
from sqlalchemy import select, func

from app.core.config import settings
from app.core.security import get_password_hash, verify_password, decode_access_token, create_access_token
from app.models.auth_rate_limit import AuthRateLimit
from app.models.otp import UserOtp
from app.modules.auth import rate_limits
from app.modules.auth.service import AuthService
from app.modules.auth.schemas import RegisterRequest
from app.modules.auth.router import ResetPasswordRequest
from test_full_audit_auth import audit_client, student  # noqa: F401


async def credentials(db, password='Original password '):
    user = await student(db)
    user.password_hash = get_password_hash(password)
    await db.commit()
    return user


async def login(client, email, password='Original password '):
    return await client.post('auth/login', json={'email': email, 'password': password})


def bearer(token):
    return {'Authorization': f'Bearer {token}'}


@pytest.mark.asyncio
async def test_logout_revokes_both_sessions_and_new_login_works(audit_client, db_session):
    user = await credentials(db_session)
    first = (await login(audit_client, user.email)).json()['access_token']
    second = (await login(audit_client, user.email)).json()['access_token']
    assert first != second
    assert (await audit_client.get('auth/me', headers=bearer(first))).status_code == 200
    other = await credentials(db_session)
    other_token = (await login(audit_client, other.email)).json()['access_token']
    assert (await audit_client.post('auth/logout', headers=bearer(first))).status_code == 200
    for token in [first, second]:
        assert (await audit_client.get('auth/me', headers=bearer(token))).status_code == 401
    assert (await audit_client.post('auth/logout', headers=bearer(second))).status_code == 401
    assert (await audit_client.get('auth/me', headers=bearer(other_token))).status_code == 200
    fresh = (await login(audit_client, user.email)).json()['access_token']
    assert decode_access_token(fresh)['sv'] == 1
    assert (await audit_client.get('auth/me', headers=bearer(fresh))).status_code == 200


@pytest.mark.asyncio
async def test_reset_revokes_sessions_and_every_pre_reset_code(audit_client, db_session):
    user = await credentials(db_session)
    token = (await login(audit_client, user.email)).json()['access_token']
    login_code, *_ = await AuthService.request_otp(db_session, user.email)
    reset_code, *_ = await AuthService.forgot_password(db_session, user.email)
    user.managed_password_enc = 'old-managed-secret'
    await db_session.commit()
    replacement = '  New password 🔐' + '界' * 70 + '  '
    result = await audit_client.post('auth/reset-password', json={
        'email': user.email, 'code': reset_code, 'new_password': replacement})
    assert result.status_code == 200
    assert user.managed_password_enc is None
    assert (await audit_client.get('auth/me', headers=bearer(token))).status_code == 401
    assert (await audit_client.post('auth/verify-otp', json={'email': user.email, 'otp': login_code})).status_code == 400
    assert (await login(audit_client, user.email)).status_code == 401
    fresh = await login(audit_client, user.email, replacement)
    assert fresh.status_code == 200
    assert decode_access_token(fresh.json()['access_token'])['sv'] == 1
    assert (await login(audit_client, user.email, replacement.strip())).status_code == 401
    assert await db_session.scalar(select(func.count()).select_from(UserOtp).where(
        UserOtp.user_id == user.id, UserOtp.verified_at.is_(None))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [None, '0', False, -1, 1])
async def test_unversioned_and_invalid_version_tokens_fail(audit_client, db_session, version):
    user = await credentials(db_session)
    data = {'sub': str(user.id), 'exp': 4000000000}
    if version is not None:
        data['sv'] = version
    token = jwt.encode(data, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    assert (await audit_client.get('auth/me', headers=bearer(token))).status_code == 401


@pytest.mark.asyncio
async def test_safe_legacy_bcrypt_login_rehashes_without_session_revocation(audit_client, db_session):
    user = await credentials(db_session)
    user.password_hash = bcrypt.hashpw(b'legacy password', bcrypt.gensalt(rounds=4)).decode()
    await db_session.commit()
    before = user.password_hash
    assert (await login(audit_client, user.email, 'wrong')).status_code == 401
    assert user.password_hash == before
    result = await login(audit_client, user.email, 'legacy password')
    assert result.status_code == 200
    assert user.password_hash.startswith('$argon2id$')
    assert verify_password('legacy password', user.password_hash)
    assert decode_access_token(result.json()['access_token'])['sv'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix', ['a' * 72, '界' * 24])
async def test_ambiguous_legacy_long_password_requires_recovery(audit_client, db_session, prefix):
    user = await credentials(db_session)
    user.password_hash = bcrypt.hashpw(prefix.encode(), bcrypt.gensalt(rounds=4)).decode()
    await db_session.commit()
    original_hash = user.password_hash
    for suffix in ['', 'original', 'impostor']:
        assert (await login(audit_client, user.email, prefix + suffix)).status_code == 401
    assert user.password_hash == original_hash
    code, *_ = await AuthService.forgot_password(db_session, user.email)
    password = prefix + 'full verified password'
    assert (await audit_client.post('auth/reset-password', json={
        'email': user.email, 'code': code, 'new_password': password})).status_code == 200
    assert (await login(audit_client, user.email, password)).status_code == 200
    assert (await login(audit_client, user.email, prefix + 'different')).status_code == 401


@pytest.mark.parametrize('password', ['x' * 8, 'x' * 128, '🔐' * 128, ' passphrase '])
def test_password_creation_contract_accepts_full_length(password):
    RegisterRequest(full_name='Name', email='test@example.com', password=password,
                    privacy_accepted=True, privacy_policy_version='2026-10-23')
    ResetPasswordRequest(email='test@example.com', code='123456', new_password=password)
    hashed = get_password_hash(password)
    assert hashed.startswith('$argon2id$')
    assert verify_password(password, hashed)
    assert not verify_password(password[:-1] + 'z', hashed)


@pytest.mark.asyncio
@pytest.mark.parametrize('password', ['x' * 7, 'x' * 129, '🔐' * 129])
async def test_register_and_reset_reject_same_policy_boundaries(audit_client, password):
    register = await audit_client.post('auth/register', json={
        'full_name': 'Name', 'email': 'boundary@example.com', 'password': password,
        'privacy_accepted': True, 'privacy_policy_version': '2026-10-23'})
    reset = await audit_client.post('auth/reset-password', json={
        'email': 'boundary@example.com', 'code': '123456', 'new_password': password})
    assert register.status_code == reset.status_code == 422


@pytest.mark.asyncio
async def test_login_quota_normalizes_account_and_survives_failed_requests(audit_client, db_session, monkeypatch):
    monkeypatch.setattr(rate_limits.time, 'time', lambda: 1800010)
    user = await credentials(db_session)
    email = user.email
    for i in range(10):
        response = await login(audit_client, f' {email.upper()} ' if i % 2 else email, 'wrong')
        assert response.status_code == 401
    blocked = await login(audit_client, email)
    assert blocked.status_code == 429
    assert 1 <= int(blocked.headers['retry-after']) <= 900
    keys = (await db_session.scalars(select(AuthRateLimit.key))).all()
    assert keys and all(len(key) == 64 and email not in key for key in keys)
    monkeypatch.setattr(rate_limits.time, 'time', lambda: 1800910)
    assert (await login(audit_client, email)).status_code == 200


@pytest.mark.asyncio
async def test_delivery_quotas_are_shared_by_registration_reset_and_otp_aliases(audit_client, db_session):
    user = await credentials(db_session)
    for endpoint in ['auth/request-otp', 'auth/otp/generate', 'auth/forgot-password', 'auth/request-otp', 'auth/forgot-password']:
        assert (await audit_client.post(endpoint, json={'email': user.email})).status_code == 200
    for endpoint in ['auth/otp/generate', 'auth/forgot-password', 'auth/register']:
        body = {'email': user.email, 'full_name': 'Name', 'password': 'new password',
                'privacy_accepted': True, 'privacy_policy_version': '2026-10-23'}
        assert (await audit_client.post(endpoint, json=body)).status_code == 429


@pytest.mark.asyncio
async def test_login_activation_does_not_bypass_delivery_limit(audit_client, db_session):
    user = await credentials(db_session)
    user.is_verified, user.account_status = False, 'PENDING_VERIFICATION'
    await db_session.commit()
    for _ in range(5):
        response = await login(audit_client, user.email)
        assert response.status_code == 200 and response.json()['requires_activation']
    assert (await login(audit_client, user.email)).status_code == 429


@pytest.mark.asyncio
async def test_resending_does_not_reset_shared_verification_budget(audit_client, db_session):
    user = await credentials(db_session)
    for _ in range(2):
        await AuthService.request_otp(db_session, user.email)
        for endpoint in ['auth/verify-otp', 'auth/otp/verify', 'auth/verify-otp', 'auth/otp/verify', 'auth/verify-otp']:
            assert (await audit_client.post(endpoint, json={'email': user.email, 'otp': '000000'})).status_code == 400
    code, *_ = await AuthService.forgot_password(db_session, user.email)
    assert (await audit_client.post('auth/reset-password', json={
        'email': user.email, 'code': code, 'new_password': 'new password'})).status_code == 429


@pytest.mark.asyncio
async def test_source_quota_survives_account_rotation_and_forwarded_spoofing(audit_client):
    for i in range(20):
        response = await audit_client.post('auth/forgot-password', json={'email': f'missing-{i}@example.com'})
        assert response.status_code == 200
    response = await audit_client.post('auth/forgot-password', json={'email': 'different@example.com'},
                                      headers={'X-Forwarded-For': '198.51.100.7'})
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_daily_delivery_quota_survives_short_window_rotation(audit_client, db_session, monkeypatch):
    user = await credentials(db_session)
    for window in range(4):
        monkeypatch.setattr(rate_limits.time, 'time', lambda w=window: 8640000 + 900 * w + 5)
        for _ in range(5):
            assert (await audit_client.post('auth/forgot-password', json={'email': user.email})).status_code == 200
    monkeypatch.setattr(rate_limits.time, 'time', lambda: 8640000 + 900 * 4 + 5)
    assert (await audit_client.post('auth/forgot-password', json={'email': user.email})).status_code == 429


@pytest.mark.asyncio
async def test_global_budget_blocks_new_keys_and_expired_rows_are_cleaned(db_session, monkeypatch):
    monkeypatch.setattr(rate_limits.time, 'time', lambda: 900001)
    await rate_limits.consume(db_session, [('global', 'probe', 1, 900), ('source', 'one', 1, 900)])
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await rate_limits.consume(db_session, [('global', 'probe', 1, 900), ('source', 'two', 1, 900)])
    assert exc.value.status_code == 429
    assert await db_session.scalar(select(func.count()).select_from(AuthRateLimit)) == 2
    monkeypatch.setattr(rate_limits.time, 'time', lambda: 901001)
    await rate_limits.consume(db_session, [('global', 'probe', 1, 900)])
    assert await db_session.scalar(select(func.count()).select_from(AuthRateLimit)) == 1
