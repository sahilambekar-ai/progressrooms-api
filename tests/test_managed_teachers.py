import uuid
import pytest
import httpx
from app.models.user import User
from test_account_roles import client, staff, auth
from app.core.config import settings

@pytest.mark.asyncio
async def test_credentials_require_reconfirmation(client, db_session):
    owner, org, _ = await staff(db_session, 'STUDIO')
    other, _, _ = await staff(db_session, 'STUDIO')
    body = dict(name='Teacher', email=f'{uuid.uuid4().hex}@example.com', password='teacher-secret', studio_id=str(org.id))
    response = await client.post('teachers', json=body, headers=auth(owner))
    assert response.status_code == 201
    record = response.json()
    path = f"teachers/{record['id']}"
    user = await db_session.get(User, uuid.UUID(record['user_id']))
    assert user.managed_password_enc and body['password'] not in user.managed_password_enc
    assert 'managed_password_enc' not in record and 'password' not in record
    assert (await client.post(path+'/password', json={'studio_password':'wrong'}, headers=auth(owner))).status_code == 403
    confirmation = {'studio_password':'strong-password'}
    assert (await client.post(path+'/password', json=confirmation, headers=auth(other))).status_code == 403
    shown = await client.post(path+'/password', json=confirmation, headers=auth(owner))
    assert shown.json()['password'] == body['password'] and shown.headers['cache-control'] == 'no-store'
    assert (await client.put(path, json={'name':'Invalid'}, headers=auth(owner))).status_code == 403
    assert (await client.put(path, json={**confirmation,'password':'new-teacher-secret'}, headers=auth(owner))).status_code == 200
    assert (await client.post(path+'/password', json=confirmation, headers=auth(owner))).json()['password'] == 'new-teacher-secret'
    assert (await client.post('auth/request-otp', json={'email': body['email']})).status_code == 403

@pytest.mark.asyncio
async def test_invitation_once_then_on_changed_details(client, db_session, monkeypatch):
    monkeypatch.setattr(settings,'BREVO_ENABLED', True)
    monkeypatch.setattr(settings,'BREVO_API_KEY', 'test-key')
    owner, org, _ = await staff(db_session, 'STUDIO')
    response = await client.post('teachers',headers=auth(owner),json=dict(name='Teacher',email=f'{uuid.uuid4().hex}@example.com',password='teacher-secret',studio_id=str(org.id)))
    path = f"teachers/{response.json()['id']}"
    deliveries=[]
    original=httpx.AsyncClient.post
    async def dispatch(self,url,**kwargs):
        if str(url).startswith('https://api.brevo.com'):
            deliveries.append(kwargs['json'])
            return httpx.Response(201,json={'messageId':'test'})
        return await original(self,url,**kwargs)
    monkeypatch.setattr(httpx.AsyncClient,'post',dispatch)
    confirmation={'studio_password':'strong-password'}
    for _ in range(2):
        assert (await client.post(path+'/invitation',headers=auth(owner),json=confirmation)).status_code == 200
    assert len(deliveries)==1 and 'teacher-secret' in deliveries[0]['htmlContent']
    for _ in range(2):
        assert (await client.put(path,headers=auth(owner),json={**confirmation,'name':'Updated'})).status_code==200
    assert len(deliveries)==2
