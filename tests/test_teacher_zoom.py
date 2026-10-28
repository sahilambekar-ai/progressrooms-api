import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from urllib.parse import urlparse, parse_qs
from unittest.mock import AsyncMock
import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select
from app.core.config import settings
from app.main import app, api_v1
from app.core.database import get_db
from app.core.security import create_access_token
from app.models.user import User
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.models.session import Session, ClassType
from app.models.schedule import ClassOccurrence
from app.models.zoom import TeacherZoomConnection, ZoomOAuthAttempt
from app.modules.zoom import service as svc
from app.modules.zoom.provider import ZoomProvider, ProviderError, mode, unseal
from app.modules.zoom.meetings import meeting_access, synchronize
from app.modules.zoom.router import connect, CallbackRequest, oauth_callback, confirm, ConfirmRequest, DefaultsRequest, save_defaults, get_defaults


@pytest.fixture
def zoom_config(monkeypatch):
    for name, value in {"APP_ENV": "test", "ZOOM_PROVIDER_MODE": "mock", "ZOOM_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                        "ZOOM_CLIENT_ID": "mock-platform-client", "ZOOM_CLIENT_SECRET": "mock-platform-secret", "ZOOM_WEBHOOK_SECRET": "mock-webhook-secret",
                        "ZOOM_REDIRECT_URI": "http://localhost:3000/integrations/zoom/callback"}.items():
        monkeypatch.setattr(settings, name, value)


@pytest_asyncio.fixture
async def studio(db_session, zoom_config):
    owner = User(email=f'{uuid.uuid4()}@example.com', full_name='Owner', role='STUDIO', account_status='ACTIVE', is_verified=True)
    teacher = User(email=f'{uuid.uuid4()}@example.com', full_name='Teacher', role='TEACHER', account_status='ACTIVE', is_verified=True)
    org = Organization(name='Zoom studio', slug=str(uuid.uuid4()))
    db_session.add_all([owner, teacher, org]); await db_session.flush()
    om = OrganizationMember(organization_id=org.id, user_id=owner.id, role='STUDIO', can_teach=True)
    tm = OrganizationMember(organization_id=org.id, user_id=teacher.id, role='TEACHER', can_teach=True)
    db_session.add_all([om, tm, OrganizationSetting(organization_id=org.id, zoom_auto_meeting_enabled=False)]); await db_session.commit()
    return org, owner, teacher, om, tm


async def authorize(db, org, member, user):
    data = await connect(org.id, member.id, user, db)
    params = parse_qs(urlparse(data['authorization_url']).query)
    candidate = await oauth_callback(CallbackRequest(state=params['state'][0], code=params['code'][0]), user, db)
    result = await confirm(ConfirmRequest(confirmation_id=candidate['confirmation_id']), user, db)
    return result, params, candidate


async def occurrence(db, org, member, minutes=0):
    ct = ClassType(organization_id=org.id, name='Yoga', slug=str(uuid.uuid4()))
    db.add(ct); await db.flush()
    sess = Session(organization_id=org.id, class_type_id=ct.id, name='Zoom class', slug=str(uuid.uuid4()))
    db.add(sess); await db.flush()
    start = svc.now() + timedelta(days=1, minutes=minutes)
    occ = ClassOccurrence(organization_id=org.id, session_id=sess.id, instructor_id=member.id, scheduled_start_at=start,
        scheduled_end_at=start+timedelta(minutes=45), actual_start_at=start, actual_end_at=start+timedelta(minutes=45), status='SCHEDULED')
    db.add(occ); await db.commit()
    return occ


async def enable(db, org, member):
    prefs = await svc.settings_for(db, org.id)
    return await save_defaults(db, org.id, DefaultsRequest(version=prefs.zoom_settings_version, default_host_member_id=member.id, auto_meeting_enabled=True, acknowledge_impact=True))


@pytest.mark.asyncio
async def test_ten_independent_grants_and_encrypted_serialization(db_session, studio):
    org, owner, teacher, om, tm = studio
    states = []
    for i in range(10):
        if i == 0:
            member, user = om, owner
        elif i == 1:
            member, user = tm, teacher
        else:
            user = User(email=f'{uuid.uuid4()}@example.com', full_name=f'Teacher {i}', role='TEACHER', account_status='ACTIVE')
            db_session.add(user); await db_session.flush()
            member = OrganizationMember(organization_id=org.id, user_id=user.id, role='TEACHER')
            db_session.add(member); await db_session.commit()
        state, _, _ = await authorize(db_session, org, member, user)
        assert state['zoom_connection_status'] == 'SIMULATED' and not state['production_ready']
        states.append(state)
    conns = (await db_session.scalars(select(TeacherZoomConnection).where(TeacherZoomConnection.organization_id == org.id))).all()
    assert len(conns) == len({c.zoom_user_id for c in conns}) == 10
    assert len({unseal(c.tokens_enc)['access_token'] for c in conns}) == 10
    for conn in conns:
        assert 'mock-access' not in conn.tokens_enc
    encoded = json.dumps(await get_defaults(db_session, org.id))
    assert not any(key in encoded for key in ('tokens_enc', 'refresh_token', 'access_token', 'host_key', 'client_secret'))


@pytest.mark.asyncio
async def test_consent_tenant_expiry_and_replay(db_session, studio):
    org, owner, teacher, om, tm = studio
    with pytest.raises(HTTPException) as error:
        await connect(org.id, tm.id, owner, db_session)
    assert error.value.status_code == 403
    with pytest.raises(HTTPException):
        await svc.authorize_member(db_session, uuid.uuid4(), tm.id, teacher)
    data = await connect(org.id, tm.id, teacher, db_session)
    query = parse_qs(urlparse(data['authorization_url']).query)
    payload = CallbackRequest(state=query['state'][0], code=query['code'][0])
    with pytest.raises(HTTPException) as error:
        await oauth_callback(payload, owner, db_session)
    assert error.value.status_code == 403
    result = await oauth_callback(payload, teacher, db_session)
    with pytest.raises(HTTPException):
        await oauth_callback(payload, teacher, db_session)
    assert await svc.connection_for(db_session, tm.id) is None  # Identity must be confirmed first.
    await confirm(ConfirmRequest(confirmation_id=result['confirmation_id']), teacher, db_session)
    with pytest.raises(HTTPException):
        await confirm(ConfirmRequest(confirmation_id=result['confirmation_id']), teacher, db_session)
    state, attempt = await svc.create_attempt(db_session, tm, teacher, 'OAUTH')
    attempt.expires_at = svc.now() - timedelta(seconds=1); await db_session.commit()
    with pytest.raises(HTTPException):
        await svc.callback(db_session, state, 'expired', teacher)
    state, attempt = await svc.create_attempt(db_session, tm, teacher, 'OAUTH')
    tm.is_active = False; await db_session.commit()
    with pytest.raises(HTTPException):
        await svc.callback(db_session, state, 'inactive', teacher)


@pytest.mark.asyncio
async def test_fallback_snapshot_host_authorization_conflicts_and_refresh(db_session, studio, monkeypatch):
    org, owner, teacher, om, tm = studio
    await authorize(db_session, org, om, owner)
    await enable(db_session, org, om)
    occ = await occurrence(db_session, org, tm)
    guest = await meeting_access(db_session, occ, org)
    assert guest['host_url'] is None and guest['target_url'] == guest['join_url'] and not guest['can_start']
    assert occ.zoom_host_member_id == om.id and occ.host_url is None
    other_teacher = await meeting_access(db_session, occ, org, is_host=True, actor_user_id=teacher.id)
    assert other_teacher['host_url'] is None and not other_teacher['can_start']
    own = await meeting_access(db_session, occ, org, is_host=True, actor_user_id=owner.id)
    assert own['host_url'] and own['can_start'] and own['is_simulated']
    assert own['host_email'] == occ.zoom_host_email
    overlap = await occurrence(db_session, org, tm, minutes=10)
    with pytest.raises(HTTPException) as error:
        await meeting_access(db_session, overlap, org)
    assert error.value.status_code == 409
    await authorize(db_session, org, tm, teacher)
    second = await meeting_access(db_session, overlap, org)
    assert second['meeting_id'] != own['meeting_id'] and overlap.zoom_host_member_id == tm.id
    prefs = await svc.settings_for(db_session, org.id)
    old_snapshot = dict(occ.zoom_settings_snapshot)
    await save_defaults(db_session, org.id, DefaultsRequest(version=prefs.zoom_settings_version, default_host_member_id=tm.id, auto_meeting_enabled=True, waiting_room=False, acknowledge_impact=True))
    again = await meeting_access(db_session, occ, org)
    assert occ.zoom_settings_snapshot == old_snapshot and again['host_email'] == own['host_email']
    conn = await svc.connection_for(db_session, om.id)
    old_token = unseal(conn.tokens_enc)['access_token']; old_version = conn.token_version
    conn.expires_at = svc.now() - timedelta(seconds=1); await db_session.commit()
    assert await svc.access_token(db_session, conn) != old_token
    assert conn.token_version == old_version + 1
    await db_session.commit()
    monkeypatch.setattr(ZoomProvider, 'refresh', AsyncMock(side_effect=ProviderError('invalid', invalid_grant=True)))
    conn.expires_at = svc.now() - timedelta(seconds=1); await db_session.commit()
    with pytest.raises(HTTPException):
        await svc.access_token(db_session, conn)
    assert conn.status == 'RECONNECT_REQUIRED' and conn.tokens_enc is None
    with pytest.raises(HTTPException):
        await meeting_access(db_session, occ, org)


@pytest.mark.asyncio
async def test_ambiguous_create_requires_reconciliation_and_never_duplicates(db_session, studio, monkeypatch):
    org, owner, teacher, om, tm = studio
    await authorize(db_session, org, om, owner); await enable(db_session, org, om)
    occ = await occurrence(db_session, org, om)
    create = AsyncMock(side_effect=ProviderError('timeout', ambiguous=True))
    monkeypatch.setattr(ZoomProvider, 'create', create)
    for _ in range(2):
        with pytest.raises(HTTPException):
            await meeting_access(db_session, occ, org)
    assert create.await_count == 1 and occ.zoom_provisioning_status == 'UNCERTAIN'
    from app.modules.zoom.router import reconcile, ReconcileRequest
    monkeypatch.setattr(ZoomProvider, 'reconcile', AsyncMock(return_value={'id': 'provider-success', 'join_url': 'https://example.invalid/join'}))
    result = await reconcile(org.id, occ.id, ReconcileRequest(), owner, (org, om), db_session)
    assert result['status'] == 'RECONCILED'
    assert (await meeting_access(db_session, occ, org))['meeting_id'] == 'provider-success'


@pytest.mark.asyncio
async def test_disconnect_impact_purge_and_history(db_session, studio):
    org, owner, teacher, om, tm = studio
    await authorize(db_session, org, om, owner); await enable(db_session, org, om)
    occ = await occurrence(db_session, org, tm)
    before = await meeting_access(db_session, occ, org)
    preview = await svc.impact(db_session, org.id, om.id)
    assert preview['future_class_count'] == 1 and preview['is_default_host']
    with pytest.raises(HTTPException):
        await svc.disconnect(db_session, om, False)
    await svc.disconnect(db_session, om, True)
    conn = await svc.connection_for(db_session, om.id)
    assert conn.tokens_enc is None and conn.status == 'DISCONNECTED'
    assert occ.provider_meeting_id == before['meeting_id'] and occ.meeting_url == before['join_url']
    prefs = await svc.settings_for(db_session, org.id)
    assert not prefs.zoom_auto_meeting_enabled and prefs.default_zoom_host_member_id is None
    assert (await svc.process_revocations(db_session, org.id))['pending'] == 0
    with pytest.raises(HTTPException):
        await meeting_access(db_session, occ, org)


@pytest.mark.asyncio
async def test_signed_webhooks_request_links_and_no_cross_teacher_binding(db_session, studio):
    org, owner, teacher, om, tm = studio
    await authorize(db_session, org, tm, teacher)
    async def dependency(): yield db_session
    api_v1.dependency_overrides[get_db] = dependency
    def auth(user): return {'Authorization': 'Bearer ' + create_access_token({'sub': str(user.id)})}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test/api/v1') as client:
            path = f'/organizations/{org.id}/teachers/{tm.id}/zoom'
            assert (await client.post(path+'/connect', headers=auth(owner))).status_code == 403
            request = (await client.post(path+'/authorization-request', headers=auth(owner))).json()
            state = parse_qs(urlparse(request['request_url']).query)['request'][0]
            assert (await client.post('/integrations/zoom/request', json={'request': state}, headers=auth(owner))).status_code == 403
            assert (await client.post('/integrations/zoom/request', json={'request': state}, headers=auth(teacher))).status_code == 200
            assert (await client.post('/integrations/zoom/request', json={'request': state}, headers=auth(teacher))).status_code == 400
            conn = await svc.connection_for(db_session, tm.id)
            data = {'event': 'app_deauthorized', 'event_ts': int(svc.now().timestamp()*1000)+1,
                    'payload': {'client_id': settings.ZOOM_CLIENT_ID, 'user_id': conn.zoom_user_id, 'account_id': conn.zoom_account_id}}
            raw = json.dumps(data).encode(); stamp = str(int(svc.now().timestamp()))
            signature = 'v0=' + hmac.new(settings.ZOOM_WEBHOOK_SECRET.encode(), b'v0:'+stamp.encode()+b':'+raw, hashlib.sha256).hexdigest()
            headers = {'Content-Type': 'application/json', 'x-zm-request-timestamp': stamp, 'x-zm-signature': signature}
            assert (await client.post('/integrations/zoom/deauthorization', content=raw, headers={**headers, 'x-zm-signature': 'forged'})).status_code == 401
            for _ in range(2):
                assert (await client.post('/integrations/zoom/deauthorization', content=raw, headers=headers)).status_code == 200
            await db_session.refresh(conn)
            assert conn.status == 'RECONNECT_REQUIRED' and conn.tokens_enc is None
            assert (await client.post('/integrations/zoom/deauthorization', content=raw, headers={**headers, 'x-zm-request-timestamp': '1'})).status_code == 401
            # Endpoint validation uses the same cryptographic verification.
            raw = json.dumps({'event': 'endpoint.url_validation', 'payload': {'plainToken': 'challenge'}}).encode()
            signature = 'v0=' + hmac.new(settings.ZOOM_WEBHOOK_SECRET.encode(), b'v0:'+stamp.encode()+b':'+raw, hashlib.sha256).hexdigest()
            response = await client.post('/integrations/zoom/deauthorization', content=raw, headers={**headers, 'x-zm-signature': signature})
            assert response.json()['encryptedToken'] == hmac.new(settings.ZOOM_WEBHOOK_SECRET.encode(), b'challenge', hashlib.sha256).hexdigest()
    finally:
        api_v1.dependency_overrides.clear()


def test_production_rejects_client_enabled_mock(zoom_config, monkeypatch):
    monkeypatch.setattr(settings, 'APP_ENV', 'production')
    with pytest.raises(HTTPException): mode()


@pytest.mark.asyncio
async def test_live_adapter_http_contract_without_real_keys(zoom_config, studio, monkeypatch):
    org, owner, teacher, om, tm = studio
    monkeypatch.setattr(settings, 'ZOOM_PROVIDER_MODE', 'live')
    monkeypatch.setattr(settings, 'ZOOM_REDIRECT_URI', 'https://app.example.com/integrations/zoom/callback')
    original_client = httpx.AsyncClient
    requests = []
    def transport(request):
        requests.append(request)
        if request.url.host == 'zoom.us':
            assert request.headers['authorization'].startswith('Basic ')
            assert 'client_secret' not in str(request.url)
            return httpx.Response(200, json={'access_token': 'live-shaped-test-access', 'refresh_token': 'live-shaped-test-refresh', 'expires_in': 3600, 'scope': 'user:read:user meeting:write:meeting meeting:read:meeting meeting:read:list_meetings meeting:update:meeting meeting:delete:meeting'})
        assert request.headers['authorization'] == 'Bearer live-shaped-test-access'
        if request.url.path == '/v2/users/me':
            return httpx.Response(200, json={'id': 'real-shaped-user', 'account_id': 'real-shaped-account', 'email': 'verified@example.com'})
        if request.method == 'POST':
            data = json.loads(request.content)
            assert '[PR:operation]' in data['topic']
            assert data['settings']['auto_recording'] == 'none'
            return httpx.Response(201, json={'id': 1234567890123, 'join_url': 'https://example.invalid/j', 'start_url': 'https://example.invalid/private-start'})
        if request.method in {'PATCH', 'DELETE'}:
            return httpx.Response(204)
        return httpx.Response(200, json={'id': 1234567890123, 'start_url': 'https://example.invalid/fresh-private-start'})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs))
    provider = ZoomProvider()
    token, identity = await provider.exchange('test-code', 'test-state', om)
    assert identity['email'] == 'verified@example.com'
    assert b'grant_type=authorization_code' in requests[0].content
    assert b'redirect_uri=' in requests[0].content
    await provider.refresh(token)
    assert b'grant_type=refresh_token' in requests[-1].content
    data = await provider.create(token['access_token'], {'topic': 'Class', 'settings': {'auto_recording': 'none'}}, 'operation')
    assert str(data['id']) == '1234567890123'
    assert (await provider.meeting(token['access_token'], data['id']))['start_url'].endswith('fresh-private-start')
    await provider.update(token['access_token'], data['id'], {'duration': 45})
    await provider.delete(token['access_token'], data['id'])
    await provider.revoke(token)
    assert b'token=live-shaped-test-access' in requests[-1].content


@pytest.mark.asyncio
async def test_reschedule_cancel_and_replacement_use_saved_connection(db_session, studio, monkeypatch):
    org, owner, teacher, om, tm = studio
    await authorize(db_session, org, om, owner); await enable(db_session, org, om)
    occ = await occurrence(db_session, org, tm)
    await meeting_access(db_session, occ, org)
    update = AsyncMock(); delete = AsyncMock()
    monkeypatch.setattr(ZoomProvider, 'update', update); monkeypatch.setattr(ZoomProvider, 'delete', delete)
    await synchronize(db_session, occ, 'reschedule', start=occ.actual_start_at+timedelta(hours=2), end=occ.actual_end_at+timedelta(hours=2))
    assert update.await_count == 1
    await synchronize(db_session, occ, 'cancel')
    assert delete.await_count == 1
    await authorize(db_session, org, tm, teacher)
    from app.modules.zoom.router import reconcile, ReconcileRequest
    old_meeting = occ.provider_meeting_id
    preview = await reconcile(org.id, occ.id, ReconcileRequest(), owner, (org, om), db_session)
    assert preview['old_host'] == occ.zoom_host_email
    result = await reconcile(org.id, occ.id, ReconcileRequest(replacement_member_id=tm.id, confirm_links_change=True), owner, (org, om), db_session)
    assert result['links_will_change']
    after = await meeting_access(db_session, occ, org)
    assert after['meeting_id'] != old_meeting and occ.zoom_host_member_id == tm.id
    from app.models.schedule import ClassOccurrenceChange
    audit = await db_session.scalar(select(ClassOccurrenceChange).where(ClassOccurrenceChange.class_occurrence_id == occ.id))
    assert old_meeting in audit.reason


@pytest.mark.asyncio
async def test_mode_cutover_can_clear_mock_revocations_without_live_calls(db_session, studio, monkeypatch):
    org, owner, teacher, om, tm = studio
    await authorize(db_session, org, om, owner)
    await svc.disconnect(db_session, om, True)
    monkeypatch.setattr(settings, 'ZOOM_PROVIDER_MODE', 'live')
    monkeypatch.setattr(settings, 'ZOOM_REDIRECT_URI', 'https://app.example.com/integrations/zoom/callback')
    revoke = AsyncMock(side_effect=AssertionError('Mock grants must never call live Zoom'))
    monkeypatch.setattr(ZoomProvider, 'revoke', revoke)
    assert (await svc.process_revocations(db_session, org.id))['pending'] == 0
    assert revoke.await_count == 0


@pytest.mark.asyncio
async def test_partial_future_updates_and_account_restricted_settings(db_session, studio, monkeypatch):
    org, owner, teacher, om, tm = studio
    await authorize(db_session, org, om, owner); await enable(db_session, org, om)
    original = ZoomProvider.create
    async def restricted(self, token, payload, operation):
        data = await original(self, token, payload, operation)
        data['settings'] = dict(payload['settings'], waiting_room=False)
        return data
    monkeypatch.setattr(ZoomProvider, 'create', restricted)
    first = await occurrence(db_session, org, tm)
    response = await meeting_access(db_session, first, org)
    assert response['restricted_settings'] == ['waiting_room']
    second = await occurrence(db_session, org, tm, minutes=120)
    await meeting_access(db_session, second, org)
    from app.modules.zoom.router import apply_future, ApplyRequest
    prefs = await svc.settings_for(db_session, org.id)
    org_id, version = org.id, prefs.zoom_settings_version
    preview = await apply_future(org_id, ApplyRequest(version=version), (org, om), db_session)
    assert preview['count'] == 2 and all(r['status'] == 'PREVIEW' for r in preview['results'])
    monkeypatch.setattr(ZoomProvider, 'update', AsyncMock(side_effect=[ProviderError('Provider unavailable'), {}]))
    result = await apply_future(org_id, ApplyRequest(version=version, apply=True), (org, om), db_session)
    assert sorted(r['status'] for r in result['results']) == ['FAILED', 'UPDATED']


@pytest.mark.asyncio
async def test_duplicate_verified_zoom_identity_cannot_bind_another_member(db_session, studio, monkeypatch):
    org, owner, teacher, om, tm = studio
    await authorize(db_session, org, om, owner)
    owner_connection = await svc.connection_for(db_session, om.id)
    tokens = ZoomProvider().mock_tokens(owner_connection.zoom_user_id)
    monkeypatch.setattr(ZoomProvider, 'exchange', AsyncMock(return_value=(tokens, {'id': owner_connection.zoom_user_id,
        'account_id': owner_connection.zoom_account_id, 'email': owner_connection.email})))
    response = await connect(org.id, tm.id, teacher, db_session)
    query = parse_qs(urlparse(response['authorization_url']).query)
    candidate = await oauth_callback(CallbackRequest(state=query['state'][0], code=query['code'][0]), teacher, db_session)
    with pytest.raises(HTTPException) as error:
        await confirm(ConfirmRequest(confirmation_id=candidate['confirmation_id']), teacher, db_session)
    assert error.value.status_code == 409
    assert await svc.connection_for(db_session, tm.id) is None
    assert owner_connection.status == 'SIMULATED'


@pytest.mark.asyncio
async def test_batch_delete_cancels_provider_and_retains_meeting_history(db_session, studio, monkeypatch):
    org, owner, teacher, om, tm = studio
    await authorize(db_session, org, om, owner); await enable(db_session, org, om)
    occ = await occurrence(db_session, org, om)
    await meeting_access(db_session, occ, org)
    original_meeting_id = occ.provider_meeting_id
    remove = AsyncMock()
    monkeypatch.setattr(ZoomProvider, 'delete', remove)
    from app.modules.classes.router import delete_session
    await delete_session(org.id, occ.session_id, (org, om), db_session)
    await db_session.refresh(occ)
    assert remove.await_count == 1 and occ.status == 'CANCELLED'
    assert occ.provider_meeting_id == original_meeting_id


@pytest.mark.asyncio
async def test_student_response_never_contains_host_access(db_session, studio, monkeypatch):
    org, owner, teacher, om, tm = studio
    await authorize(db_session, org, om, owner); await enable(db_session, org, om)
    occ = await occurrence(db_session, org, tm)
    start = svc.now()+timedelta(minutes=2)
    occ.actual_start_at = start; occ.actual_end_at = start+timedelta(minutes=45)
    student = User(email=f'{uuid.uuid4()}@example.com', full_name='Student', role='STUDENT', account_status='ACTIVE')
    db_session.add(student); await db_session.flush()
    from app.models.commerce import SessionEnrollment
    db_session.add(SessionEnrollment(organization_id=org.id, session_id=occ.session_id, user_id=student.id, status='ACTIVE',
        valid_from=svc.now()-timedelta(days=1), valid_until=svc.now()+timedelta(days=30), total_sessions=10))
    await db_session.commit()
    monkeypatch.setattr(ZoomProvider, 'meeting', AsyncMock(side_effect=AssertionError('Participant request must never fetch a start URL')))
    from app.modules.student.router import get_class_meeting_url
    result = await get_class_meeting_url(occ.id, student, db_session)
    assert set(result) == {'meeting_url', 'provider', 'is_simulated'}
    assert 'host=1' not in result['meeting_url'] and occ.host_url is None

@pytest.mark.asyncio
async def test_studio_direct_authorization_reconfirms_password(db_session, studio):
    from app.core.security import get_password_hash
    from app.modules.zoom.router import studio_connect, StudioConnectRequest
    org, owner, teacher, om, tm = studio
    owner.password_hash = get_password_hash('studio-password')
    await db_session.commit()
    with pytest.raises(HTTPException) as denied:
        await studio_connect(org.id, tm.id, StudioConnectRequest(studio_password='wrong'), owner, db_session)
    assert denied.value.status_code == 403
    response = await studio_connect(org.id, tm.id, StudioConnectRequest(studio_password='studio-password'), owner, db_session)
    params = parse_qs(urlparse(response['authorization_url']).query)
    candidate = await oauth_callback(CallbackRequest(state=params['state'][0], code=params['code'][0]), owner, db_session)
    await confirm(ConfirmRequest(confirmation_id=candidate['confirmation_id']), owner, db_session)
    assert (await svc.connection_for(db_session, tm.id)).member_id == tm.id

@pytest.mark.asyncio
async def test_request_link_without_provider_configuration(db_session, studio, monkeypatch):
    from app.modules.zoom.router import request_authorization
    org, owner, teacher, om, tm = studio
    monkeypatch.setattr(settings, 'ZOOM_PROVIDER_MODE', 'disabled')
    monkeypatch.setattr(settings, 'ZOOM_TOKEN_ENCRYPTION_KEY', '')
    response = await request_authorization(org.id, tm.id, owner, db_session)
    assert 'request=' in response['request_url']


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized_id,accepted", [("Host_123-abc", True), ("different_host", False)])
async def test_requested_host_id_is_verified_against_zoom_identity(db_session, studio, monkeypatch, authorized_id, accepted):
    org, owner, teacher, om, tm = studio
    state, attempt = await svc.create_attempt(db_session, om, owner, "OAUTH", expected_host_id="Host_123-abc")
    monkeypatch.setattr(ZoomProvider, "exchange", AsyncMock(return_value=(
        {"access_token": "mock-token", "refresh_token": "mock-refresh"},
        {"id": authorized_id, "email": "separate-address@example.com", "display_name": "Host"},
    )))
    if accepted:
        result = await svc.callback(db_session, state, "mock-code", owner)
        assert result["confirmation_id"] == str(attempt.id)
    else:
        with pytest.raises(HTTPException) as error:
            await svc.callback(db_session, state, "mock-code", owner)
        assert error.value.status_code == 422
        assert "Host ID" in error.value.detail
    assert attempt.expected_host_id == "Host_123-abc" and attempt.expected_email is None


@pytest.mark.asyncio
async def test_connect_accepts_host_id_without_email_validation(db_session, studio):
    org, owner, teacher, om, tm = studio
    async def override():
        yield db_session
    api_v1.dependency_overrides[get_db] = override
    headers = {"Authorization": f"Bearer {create_access_token({'sub': str(owner.id)})}"}
    path = f"/api/v1/organizations/{org.id}/teachers/{om.id}/zoom/connect"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post(path, params={"zoom_host_id": "Host_123-abc"}, headers=headers)
            assert result.status_code == 200, result.text
            state = parse_qs(urlparse(result.json()["authorization_url"]).query)["state"][0]
            attempt = await db_session.scalar(select(ZoomOAuthAttempt).where(ZoomOAuthAttempt.state_hash == svc.digest(state)))
            assert attempt.expected_host_id == "Host_123-abc" and attempt.expected_email is None
            assert (await client.post(path, params={"zoom_host_id": "email@example.com"}, headers=headers)).status_code == 422
    finally:
        api_v1.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('days,status', [(30, 'SCHEDULED'), (-2, 'SCHEDULED'), (-2, 'COMPLETED')])
async def test_owner_and_student_access_without_time_window(db_session, studio, monkeypatch, days, status):
    from app.models.commerce import SessionEnrollment
    from app.modules.student.router import get_class_meeting_url
    from app.modules.scheduling.router import create_join_link
    org, owner, teacher, om, tm = studio
    await authorize(db_session, org, om, owner)
    await enable(db_session, org, om)
    occ = await occurrence(db_session, org, om)
    start = svc.now() + timedelta(days=days)
    occ.actual_start_at, occ.actual_end_at = start, start + timedelta(minutes=45)
    occ.status = status
    if status == 'COMPLETED':
        session = await db_session.get(Session, occ.session_id)
        session.is_active, session.deleted_at = False, svc.now()
    student = User(email=f'{uuid.uuid4()}@example.com', full_name='Student', role='STUDENT', account_status='ACTIVE')
    db_session.add(student)
    await db_session.flush()
    historical = days < 0
    enrollment = SessionEnrollment(organization_id=org.id, session_id=occ.session_id, user_id=student.id,
        status='EXPIRED' if historical else 'ACTIVE', valid_from=svc.now()-timedelta(days=10),
        valid_until=svc.now()+timedelta(days=-1 if historical else 60), total_sessions=1,
        sessions_attended=1 if historical else 0)
    db_session.add(enrollment)
    await db_session.commit()
    own = await create_join_link(org.id, occ.id, (org, om), db_session)
    assert own['can_start'] and own['meeting_url'] == own['host_url']
    assert own['host_url'] != own['join_url']
    monkeypatch.setattr(ZoomProvider, 'meeting', AsyncMock(side_effect=AssertionError('Student cannot retrieve host credentials')))
    result = await get_class_meeting_url(occ.id, student, db_session)
    assert result['meeting_url'] == own['join_url']
    assert enrollment.sessions_attended == (1 if historical else 0)
    stranger = User(id=uuid.uuid4(), email='stranger@example.com', role='STUDENT')
    with pytest.raises(HTTPException) as denied:
        await get_class_meeting_url(occ.id, stranger, db_session)
    assert denied.value.status_code == 403
    occ.status = 'CANCELLED'
    await db_session.commit()
    with pytest.raises(HTTPException):
        await create_join_link(org.id, occ.id, (org, om), db_session)


@pytest.mark.asyncio
async def test_restart_expired_zoom_authorization_preserves_identity_and_consent(db_session, studio):
    org, owner, teacher, om, tm = studio
    state, old = await svc.create_attempt(db_session, tm, teacher, 'OAUTH', expected_email='teacher@example.test', expected_host_id='host-123')
    old.expires_at = svc.now() - timedelta(seconds=1)
    await db_session.commit()
    with pytest.raises(HTTPException) as denied:
        await svc.restart_authorization(db_session, state, owner, 'OAUTH')
    assert denied.value.status_code == 403
    data = await svc.restart_authorization(db_session, state, teacher, 'OAUTH')
    fresh_state = parse_qs(urlparse(data['authorization_url']).query)['state'][0]
    assert fresh_state != state
    fresh = await svc.validate_attempt(db_session, fresh_state, teacher, 'OAUTH')
    assert fresh.expected_email == 'teacher@example.test'
    assert fresh.expected_host_id == 'host-123'
    assert old.consumed_at is not None
    with pytest.raises(HTTPException):
        await svc.callback(db_session, state, 'old-code', teacher)
    tm.is_active = False
    await db_session.commit()
    with pytest.raises(HTTPException):
        await svc.restart_authorization(db_session, fresh_state, teacher, 'OAUTH')


@pytest.mark.asyncio
async def test_restart_consumed_callback_requires_fresh_exchange(db_session, studio):
    org, owner, teacher, om, tm = studio
    data = await connect(org.id, tm.id, teacher, db_session)
    query = parse_qs(urlparse(data['authorization_url']).query)
    candidate = await svc.callback(db_session, query['state'][0], query['code'][0], teacher)
    fresh = await svc.restart_authorization(db_session, query['state'][0], teacher, 'OAUTH')
    assert parse_qs(urlparse(fresh['authorization_url']).query)['state'][0] != query['state'][0]
    with pytest.raises(HTTPException):
        await svc.confirm(db_session, uuid.UUID(candidate['confirmation_id']), teacher)
    assert await svc.connection_for(db_session, tm.id) is None


@pytest.mark.asyncio
async def test_restart_expired_teacher_request_and_owner_reconfirmation(db_session, studio):
    org, owner, teacher, om, tm = studio
    state, request = await svc.create_attempt(db_session, tm, owner, 'REQUEST')
    request.expires_at = svc.now() - timedelta(seconds=1)
    await db_session.commit()
    result = await svc.restart_authorization(db_session, state, teacher, 'REQUEST')
    fresh_state = parse_qs(urlparse(result['authorization_url']).query)['state'][0]
    fresh = await svc.validate_attempt(db_session, fresh_state, teacher, 'OAUTH')
    assert fresh.intended_user_id == teacher.id and fresh.member_id == tm.id
    # Connecting on behalf of another teacher must return to the password prompt.
    owner_state, _ = await svc.create_attempt(db_session, tm, owner, 'OAUTH')
    with pytest.raises(HTTPException) as denied:
        await svc.restart_authorization(db_session, owner_state, owner, 'OAUTH')
    assert denied.value.status_code == 403
