import uuid
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import select
from app.main import app, api_v1
from app.core.database import get_db
from app.core.config import settings
from app.core.security import create_access_token
from app.models.user import User
from app.models.organization import Organization
from app.models.session import ClassType, Session, SessionPricing
from app.models.membership_tier import MembershipTier, MembershipTierClass
from app.models.commerce import SessionEnrollment
from app.modules.commerce.service import available_credit

@pytest_asyncio.fixture
async def store(db_session, monkeypatch):
    monkeypatch.setattr(settings, 'DEV_TEST_LOGIN_ENABLED', True)
    monkeypatch.setattr(settings, 'RAZORPAY_KEY_SECRET', 'rzp_secret_mock')
    user = User(email=f'{uuid.uuid4()}@example.com', full_name='Student', role='STUDENT', is_verified=True, account_status='ACTIVE')
    org = Organization(name='Store',slug=f'store-{uuid.uuid4().hex}',status='ACTIVE')
    db_session.add_all([user,org]); await db_session.flush()
    ct = ClassType(organization_id=org.id,name='Yoga',slug='yoga')
    db_session.add(ct); await db_session.flush()
    classes=[]
    for i in range(2):
        s=Session(organization_id=org.id,class_type_id=ct.id,name=f'Class {i}',slug=f'class-{i}',is_active=True,is_public=True)
        db_session.add(s); await db_session.flush()
        db_session.add(SessionPricing(session_id=s.id,price=100,currency='INR',pricing_type='PACKAGE',total_classes=2,validity_days=30))
        classes.append(s)
    tier=MembershipTier(organization_id=org.id,name='Shared pass',slug='shared',price=250,currency='INR',total_classes=3,validity_days=45,is_active=True)
    db_session.add(tier); await db_session.flush()
    for s in classes: db_session.add(MembershipTierClass(tier_id=tier.id,session_id=s.id))
    await db_session.commit()
    async def dependency(): yield db_session
    api_v1.dependency_overrides[get_db]=dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test/api/v1') as client:
        yield client,user,org,classes,tier
    api_v1.dependency_overrides.clear()

def auth(user): return {'Authorization':f"Bearer {create_access_token({'sub':str(user.id)})}"}

@pytest.mark.asyncio
async def test_pass_purchase_snapshot_idempotency_shared_credits(store, db_session):
    client,user,org,classes,tier=store
    response=await client.post('/commerce/orders',json={'tier_id':str(tier.id)},headers=auth(user))
    assert response.status_code==200,response.text
    order=response.json(); assert order['amount']==250
    assert 'refunds_supported' not in order['purchase_policy']
    assert order['purchase_policy']['version'] == '2026-10-21'
    assert 'support' not in order['purchase_policy']
    assert 'refund' not in order['purchase_policy']['terms'].lower()
    tier.price=999; tier.total_classes=99; await db_session.commit()
    body={'order_id':order['order_id'],'razorpay_order_id':order['razorpay_order_id'],
          'razorpay_payment_id':f"dev_{order['order_id'].replace('-','')}",'razorpay_signature':'development-only'}
    wrong=await client.post('/commerce/payments/verify',json={**body,'razorpay_order_id':'wrong'},headers=auth(user))
    assert wrong.status_code==400
    for _ in range(2):
        response=await client.post('/commerce/payments/verify',json=body,headers=auth(user))
        assert response.status_code==200,response.text
    enrollments=(await db_session.execute(select(SessionEnrollment).where(SessionEnrollment.user_id==user.id))).scalars().all()
    assert len(enrollments)==2
    assert all(e.total_sessions==3 for e in enrollments)
    assert sum(e.billing_value for e in enrollments)==250
    enrollments[0].sessions_attended=2; enrollments[1].sessions_attended=1
    await db_session.commit()
    assert not await available_credit(db_session,enrollments[0])
    dashboard=await client.get('/student/enrollments',headers=auth(user))
    assert all(e['sessions_attended']==3 for e in dashboard.json())

@pytest.mark.asyncio
async def test_catalog_filters_and_checkout_rechecks(store,db_session):
    client,user,org,classes,tier=store
    classes[0].is_active=False; tier.is_active=False; await db_session.commit()
    response=await client.get(f'/public/organizations/{org.slug}')
    assert response.status_code==200,response.text
    assert len(response.json()['sessions'])==1
    assert response.json()['passes']==[]
    for body in [{'session_id':str(classes[0].id)},{'tier_id':str(tier.id)}]:
        assert (await client.post('/commerce/orders',json=body,headers=auth(user))).status_code==400
    assert (await client.post('/commerce/orders',json={'session_id':str(classes[1].id)})).status_code==401
    assert (await client.post('/commerce/orders',json={'session_id':str(classes[1].id),'tier_id':str(tier.id)},headers=auth(user))).status_code==422

@pytest.mark.asyncio
async def test_mock_checkout_requires_explicit_dev_mode(store,monkeypatch):
    client,user,org,classes,tier=store
    monkeypatch.setattr(settings,'DEV_TEST_LOGIN_ENABLED',False)
    response=await client.post('/commerce/orders',json={'session_id':str(classes[0].id)},headers=auth(user))
    assert response.status_code==400
    assert 'not configured' in response.json()['detail']

@pytest.mark.asyncio
async def test_free_class_without_payment_configuration(store,db_session,monkeypatch):
    from app.models.session import SessionPricing
    client,user,org,classes,tier=store
    monkeypatch.setattr(settings,'DEV_TEST_LOGIN_ENABLED',False)
    pricing=await db_session.scalar(select(SessionPricing).where(SessionPricing.session_id==classes[0].id))
    pricing.price=0; await db_session.commit()
    response=await client.post('/commerce/orders',json={'session_id':str(classes[0].id)},headers=auth(user))
    assert response.status_code==200,response.text
    order=response.json(); assert order['free']
    response=await client.post('/commerce/payments/verify',headers=auth(user),json={
        'order_id':order['order_id'],'razorpay_order_id':order['razorpay_order_id'],
        'razorpay_payment_id':f"free_{order['order_id'].replace('-','')}",'razorpay_signature':'free-enrollment'})
    assert response.status_code==200,response.text

@pytest.mark.asyncio
async def test_signed_webhook_recovers_purchase_and_is_idempotent(store,db_session,monkeypatch):
    import json,hmac,hashlib
    client,user,org,classes,tier=store
    order=(await client.post('/commerce/orders',json={'tier_id':str(tier.id)},headers=auth(user))).json()
    monkeypatch.setattr(settings,'RAZORPAY_WEBHOOK_SECRET','test-webhook-secret')
    payload={'event':'payment.captured','payload':{'payment':{'entity':{'id':f'pay_{uuid.uuid4().hex}',
        'order_id':order['razorpay_order_id'],'status':'captured','amount':25000,'currency':'INR'}}}}
    raw=json.dumps(payload).encode()
    assert (await client.post('/commerce/webhooks/razorpay',content=raw)).status_code==400
    signature=hmac.new(b'test-webhook-secret',raw,hashlib.sha256).hexdigest()
    for _ in range(2):
        response=await client.post('/commerce/webhooks/razorpay',content=raw,headers={'x-razorpay-signature':signature})
        assert response.status_code==200,response.text
    assert len((await client.get('/student/enrollments',headers=auth(user))).json())==2

@pytest.mark.asyncio
async def test_student_studio_selector_includes_assignments_and_enrollments_only(store,db_session):
    from app.models.organization import OrganizationStudent
    from datetime import datetime,timezone
    client,user,org,classes,tier=store
    solo=Organization(name='Solo Teacher',slug=f'solo-{uuid.uuid4().hex}',status='ACTIVE')
    unrelated=Organization(name='Unrelated',slug=f'unrelated-{uuid.uuid4().hex}',status='ACTIVE')
    inactive=Organization(name='Inactive',slug=f'inactive-{uuid.uuid4().hex}',status='INACTIVE')
    db_session.add_all([solo,unrelated,inactive]);await db_session.flush()
    db_session.add_all([OrganizationStudent(organization_id=solo.id,user_id=user.id,status='ACTIVE'),
        OrganizationStudent(organization_id=inactive.id,user_id=user.id,status='ACTIVE'),
        SessionEnrollment(organization_id=org.id,session_id=classes[0].id,user_id=user.id,status='ACTIVE',valid_from=datetime.now(timezone.utc))])
    await db_session.commit()
    response=await client.get('/student/studios',headers=auth(user))
    assert response.status_code==200
    assert {s['slug'] for s in response.json()}=={solo.slug,org.slug}
    stranger=User(email=f'{uuid.uuid4()}@example.com',full_name='Other student',role='STUDENT',is_verified=True,account_status='ACTIVE')
    db_session.add(stranger);await db_session.commit()
    assert (await client.get('/student/studios',headers=auth(stranger))).json()==[]
    assert (await client.get('/student/studios')).status_code==401

@pytest.mark.asyncio
async def test_live_class_remains_in_dashboard_after_start(store,db_session):
    from datetime import datetime,timedelta,timezone
    from app.models.schedule import ClassOccurrence
    client,user,org,classes,tier=store
    now=datetime.now(timezone.utc)
    db_session.add(SessionEnrollment(organization_id=org.id,session_id=classes[0].id,user_id=user.id,
        status='ACTIVE',valid_from=now-timedelta(days=1),valid_until=now+timedelta(days=1)))
    live=ClassOccurrence(organization_id=org.id,session_id=classes[0].id,
        scheduled_start_at=now-timedelta(minutes=5),scheduled_end_at=now+timedelta(minutes=55),
        actual_start_at=now-timedelta(minutes=5),actual_end_at=now+timedelta(minutes=55),status='SCHEDULED')
    ended=ClassOccurrence(organization_id=org.id,session_id=classes[0].id,
        scheduled_start_at=now-timedelta(hours=2),scheduled_end_at=now-timedelta(hours=1),
        actual_start_at=now-timedelta(hours=2),actual_end_at=now-timedelta(hours=1),status='SCHEDULED')
    db_session.add_all([live,ended]);await db_session.commit()
    response=await client.get('/student/upcoming-classes',headers=auth(user))
    assert response.status_code==200,response.text
    assert [c['id'] for c in response.json()]==[str(live.id)]
    assert response.json()[0]['can_join'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("offer", [None, 200, 0, 250])
async def test_membership_offer_catalog_checkout_and_student_receipt(store, db_session, offer):
    client, user, org, classes, tier = store
    tier.offer_price = offer
    await db_session.commit()
    expected = offer if offer is not None else 250
    catalog = (await client.get(f'/public/organizations/{org.slug}')).json()
    assert catalog['passes'][0]['price'] == 250
    assert catalog['passes'][0]['offer_price'] == offer
    response = await client.post('/commerce/orders', json={'tier_id': str(tier.id)}, headers=auth(user))
    assert response.status_code == 200, response.text
    order = response.json()
    assert order['amount'] == expected
    # Catalog changes must not change an existing order or student receipt.
    tier.offer_price = None
    tier.price = 500
    await db_session.commit()
    prefix, signature = ('free_', 'free-enrollment') if expected == 0 else ('dev_', 'development-only')
    response = await client.post('/commerce/payments/verify', headers=auth(user), json={
        'order_id': order['order_id'], 'razorpay_order_id': order['razorpay_order_id'],
        'razorpay_payment_id': prefix + order['order_id'].replace('-', ''), 'razorpay_signature': signature})
    assert response.status_code == 200, response.text
    dashboard = await client.get('/student/enrollments', headers=auth(user))
    assert sum(e['amount_paid'] for e in dashboard.json()) == expected
    assert all(e['currency'] == 'INR' for e in dashboard.json())


@pytest.mark.asyncio
@pytest.mark.parametrize("offer", [-1, 251])
async def test_membership_offer_database_constraint(store, db_session, offer):
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError
    tier = store[-1]
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(update(MembershipTier).where(MembershipTier.id == tier.id).values(offer_price=offer))
