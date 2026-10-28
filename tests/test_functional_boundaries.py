"""Functional contracts, foreign-tenant boundaries and real object ownership."""
import re
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import select
from app.main import api_v1
from app.models.organization import Organization, OrganizationStudent
from app.models.session import ClassType, Session, SessionInstructor, SessionPricing
from app.models.commerce import SessionEnrollment, Order, Payment
from app.models.support import SupportTicket
from app.modules.classes.schemas import SessionCreate, SessionUpdate, SessionPricingCreate, ClassTypeCreate
from app.modules.membership_tiers.schemas import MembershipTierCreate, MembershipTierUpdate
from app.modules.support.router import CreateTicketPayload
from pydantic import ValidationError
from test_account_roles import client, staff, auth  # noqa: F401


def routes(prefix):
    return [(method, route.path) for route in iter_route_contexts(api_v1.routes)
            if isinstance(route.original_route, APIRoute) and route.path.startswith(prefix)
            for method in route.methods]


async def student(db, org=None):
    user, _, _ = await staff(db, 'STUDENT')
    if org:
        db.add(OrganizationStudent(organization_id=org.id, user_id=user.id))
        await db.commit()
    return user


def path_for(template, org):
    return re.sub(r'\{[^}]+\}', lambda m: str(org.id if m[0] == '{org_id}' else uuid.uuid4()), template)


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['STUDIO', 'TEACHER', 'STUDENT'])
async def test_every_tenant_route_rejects_foreign_membership(client, db_session, role):
    user, _, _ = await staff(db_session, role)
    _, foreign, _ = await staff(db_session, 'STUDIO')
    cases = routes('/organizations/{org_id}')
    assert len(cases) >= 70
    for method, template in cases:
        response = await client.request(method, path_for(template, foreign), headers=auth(user), json={"studio_password":"strong-password"})
        assert response.status_code in {403, 404}, (method, template, response.status_code, response.text)


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['STUDIO', 'TEACHER', 'STUDENT'])
async def test_every_admin_route_rejects_non_admins(client, db_session, role, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'DEV_TEST_LOGIN_ENABLED', False)
    user, _, _ = await staff(db_session, role)
    _, foreign, _ = await staff(db_session, 'STUDIO')
    for method, template in routes('/admin/'):
        response = await client.request(method, path_for(template, foreign), headers=auth(user), json={"studio_password":"strong-password"})
        assert response.status_code == 403, (method, template, response.status_code, response.text)


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['STUDIO', 'TEACHER', 'SUPER_ADMIN'])
async def test_student_api_rejects_other_roles(client, db_session, role):
    user, _, _ = await staff(db_session, role)
    org = Organization(name='Placeholder', slug=uuid.uuid4().hex)
    for method, template in routes('/student/'):
        response = await client.request(method, path_for(template, org), headers=auth(user), json={"studio_password":"strong-password"})
        assert response.status_code == 403, (method, template, response.status_code, response.text)


@pytest.mark.asyncio
async def test_support_requires_related_studio_and_immutable_student_identity(client, db_session):
    owner, org, _ = await staff(db_session, 'STUDIO')
    _, foreign, _ = await staff(db_session, 'STUDIO')
    first = await student(db_session, org)
    second = await student(db_session)
    body = {'organization_id': str(org.id), 'subject': 'Question', 'initial_message': 'Hello', 'category': 'GENERAL'}
    assert (await client.post('student/support-tickets', headers=auth(second), json=body)).status_code == 403
    created = await client.post('student/support-tickets', headers=auth(first), json=body)
    assert created.status_code == 201
    ticket = created.json()['id']
    assert (await client.get(f'student/support-tickets/{ticket}', headers=auth(second))).status_code == 404
    assert (await client.post(f'student/support-tickets/{ticket}/messages', headers=auth(second), json={'message':'intrude'})).status_code == 404
    assert (await client.get(f'organizations/{foreign.id}/support-tickets/{ticket}', headers=auth(owner))).status_code == 403
    # Even a legacy matching email must not transfer ticket ownership.
    record = await db_session.get(SupportTicket, uuid.UUID(ticket))
    record.student_email = second.email
    await db_session.commit()
    assert (await client.get(f'student/support-tickets/{ticket}', headers=auth(second))).status_code == 404
    assert (await client.get(f'student/support-tickets/{ticket}', headers=auth(first))).status_code == 200
    assert (await client.get(f'organizations/{org.id}/support-tickets/{ticket}', headers=auth(owner))).status_code == 200
    assert (await client.post('student/support-tickets', headers=auth(first), json={**body,'organization_id':str(foreign.id)})).status_code == 403


@pytest.mark.parametrize('model,base,field,value', [
    (ClassTypeCreate, {'name':'Yoga','slug':'yoga'}, 'name', ' '),
    (SessionCreate, {'class_type_id':uuid.uuid4(),'name':'Yoga','slug':'yoga','pricing':{'price':0}}, 'duration_minutes', -1),
    (SessionCreate, {'class_type_id':uuid.uuid4(),'name':'Yoga','slug':'yoga','pricing':{'price':0}}, 'min_age', -1),
    (SessionCreate, {'class_type_id':uuid.uuid4(),'name':'Yoga','slug':'yoga','pricing':{'price':0},'max_age':10}, 'min_age', 11),
    (SessionCreate, {'class_type_id':uuid.uuid4(),'name':'Yoga','slug':'yoga','pricing':{'price':0}}, 'short_description', 'a'*501),
    (SessionUpdate, {}, 'name', 'a'*256), (SessionUpdate, {}, 'duration_minutes', 0),
    (SessionPricingCreate, {'price':0}, 'price', float('inf')),
    (SessionPricingCreate, {'price':0}, 'price', 1.234),
    (SessionPricingCreate, {'price':0}, 'validity_days', -1),
    (SessionPricingCreate, {'price':0}, 'total_classes', 0),
    (MembershipTierCreate, {'name':'Pass','price':1}, 'name', ' '),
    (MembershipTierCreate, {'name':'Pass','price':1}, 'tier_type', 'UNKNOWN'),
    (MembershipTierCreate, {'name':'Pass','price':1}, 'validity_days', 0),
    (MembershipTierCreate, {'name':'Pass','price':1}, 'total_classes', -1),
    (MembershipTierUpdate, {}, 'validity_days', 0), (MembershipTierUpdate, {}, 'name', None),
    (CreateTicketPayload, {'organization_id':uuid.uuid4(),'subject':'Question','initial_message':'Hello'}, 'subject', 'a'*256),
    (CreateTicketPayload, {'organization_id':uuid.uuid4(),'subject':'Question','initial_message':'Hello'}, 'category', 'OTHER'),
])
def test_schema_boundaries(model, base, field, value):
    with pytest.raises(ValidationError):
        model(**{**base, field:value})


@pytest.mark.asyncio
async def test_analytics_contract_is_tenant_scoped_and_excludes_unpaid_collection(client, db_session):
    owner, org, _ = await staff(db_session, 'STUDIO')
    teacher, _, _ = await staff(db_session, 'TEACHER', org)
    other, foreign, _ = await staff(db_session, 'STUDIO')
    user = await student(db_session, org)
    now = datetime.now(timezone.utc)
    ct = ClassType(organization_id=org.id,name='Yoga',slug='yoga')
    db_session.add(ct); await db_session.flush()
    session = Session(organization_id=org.id,class_type_id=ct.id,name='Yoga',slug='yoga')
    db_session.add(session); await db_session.flush()
    db_session.add(SessionPricing(session_id=session.id,price=100,currency='INR'))
    db_session.add(SessionEnrollment(organization_id=org.id,session_id=session.id,user_id=user.id,valid_from=now-timedelta(days=1),status='ACTIVE'))
    for target, amount, status in [(org, 100, 'PAID'), (org, 30, 'PENDING'), (foreign, 900, 'PAID')]:
        order = Order(organization_id=target.id,user_id=user.id,order_number=uuid.uuid4().hex,total_amount=amount,currency='INR',status=status)
        db_session.add(order); await db_session.flush()
        if status == 'PAID':
            db_session.add(Payment(order_id=order.id,amount=amount,currency='INR',status='SUCCESS',payment_provider='RAZORPAY'))
    await db_session.commit()
    endpoint=f'organizations/{org.id}/analytics'
    assert (await client.get(endpoint,headers=auth(teacher))).status_code == 403
    assert (await client.get(endpoint,headers=auth(other))).status_code == 403
    data=(await client.get(endpoint,headers=auth(owner))).json()
    assert data['active_enrolled_students']==1
    assert data['collected_revenue']==100 and data['billed_revenue']==130
    assert data['projected_subscription_value']==100
    assert data['currency']=='INR' and data['explanation']


@pytest.mark.asyncio
async def test_notification_destinations_use_existing_pages_and_honest_labels(db_session, monkeypatch):
    from app.modules.communication import billing_engine as engine
    from app.modules.communication.templates import render_billing_payment_received_email
    from app.models.communication import CommunicationLog
    owner, org, _ = await staff(db_session,'STUDIO')
    user=await student(db_session,org)
    order=Order(organization_id=org.id,user_id=user.id,order_number=uuid.uuid4().hex,total_amount=10,status='PAID')
    db_session.add(order); await db_session.commit()
    # Capture actual rendered output at the provider boundary.
    bodies=[]
    async def record(db,log,html):
        bodies.append(html); log.status='SENT'
    monkeypatch.setattr(engine,'_record_brevo_email',record)
    from app.modules.communication.service import get_or_create_settings
    setting=await get_or_create_settings(db_session);setting.email_enabled=True;await db_session.commit()
    await engine.notify_student_payment_received(db_session,order.id)
    assert bodies and '/student?tab=passes' in bodies[0] and '/receipts/' not in bodies[0]
    _, html=render_billing_payment_received_email('Owner','Studio',10,'https://app.example/dashboard?tab=billing-saas','pay_1')
    assert 'Review Payment History' in html and 'Official Receipt' not in html


@pytest.mark.parametrize('model,data', [
    (__import__('app.modules.organizations.router',fromlist=['StudioProfileUpdate']).StudioProfileUpdate, {'city':'a'*101}),
    (__import__('app.modules.organizations.router',fromlist=['StudioProfileUpdate']).StudioProfileUpdate, {'completion_percentage':101}),
    (__import__('app.modules.organizations.router',fromlist=['OrganizationCreate']).OrganizationCreate, {'name':'Studio','slug':'studio','timezone':'Invalid/Zone'}),
    (__import__('app.modules.scheduling.router',fromlist=['ReschedulePayload']).ReschedulePayload, {'new_start_at':'2026-11-01T10:00:00Z','new_end_at':'2026-11-01T09:00:00Z','reason':'Changed'}),
    (__import__('app.modules.students.router',fromlist=['StudentUpdate']).StudentUpdate, {'phone':'+91 98765 43210','status':None}),
])
def test_profile_and_schedule_contracts_reject_invalid_values(model,data):
    with pytest.raises(ValidationError):
        model(**data)


async def catalog(db,org,teacher=None,user=None):
    ct=ClassType(organization_id=org.id,name='Yoga',slug=uuid.uuid4().hex)
    db.add(ct);await db.flush()
    cls=Session(organization_id=org.id,class_type_id=ct.id,name='Yoga',slug=uuid.uuid4().hex,min_age=10,max_age=30)
    db.add(cls);await db.flush()
    db.add(SessionPricing(session_id=cls.id,price=100,currency='INR',total_classes=2,validity_days=30))
    if teacher:
        db.add(SessionInstructor(session_id=cls.id,member_id=teacher.id,role='PRIMARY'))
    if user:
        db.add(SessionEnrollment(organization_id=org.id,session_id=cls.id,user_id=user.id,status='ACTIVE',valid_from=datetime.now(timezone.utc)))
    await db.commit()
    return ct,cls


@pytest.mark.asyncio
async def test_class_and_package_object_boundaries_and_partial_age_update(client,db_session):
    owner,org,_=await staff(db_session,'STUDIO')
    teacher,_,member=await staff(db_session,'TEACHER',org)
    _,foreign,foreign_member=await staff(db_session,'STUDIO')
    other_teacher,_,_=await staff(db_session,'TEACHER',org)
    ct,cls=await catalog(db_session,org,member)
    foreign_ct,foreign_cls=await catalog(db_session,foreign)
    endpoint=f'organizations/{org.id}/classes/{cls.id}'
    assert (await client.put(endpoint,headers=auth(teacher),json={'name':'Unauthorized'})).status_code==403
    assert (await client.put(endpoint,headers=auth(other_teacher),json={'pricing':{'price':50}})).status_code==403
    assert (await client.put(endpoint,headers=auth(teacher),json={'pricing':{'price':50}})).status_code==200
    assert (await client.put(endpoint,headers=auth(owner),json={'min_age':31})).status_code==422
    assert (await client.put(endpoint,headers=auth(owner),json={'min_age':12})).status_code==200
    assert (await client.put(endpoint,headers=auth(owner),json={'instructor_member_ids':[str(foreign_member.id)]})).status_code==422
    body={'class_type_id':str(foreign_ct.id),'name':'Yoga','slug':'yoga','pricing':{'price':1}}
    assert (await client.post(f'organizations/{org.id}/classes',headers=auth(owner),json=body)).status_code==422
    tier={'name':'Pass','price':10,'class_ids':[str(foreign_cls.id)]}
    assert (await client.post(f'organizations/{org.id}/membership-tiers',headers=auth(owner),json=tier)).status_code==422


@pytest.mark.asyncio
async def test_payment_ownership_and_inconsistent_paid_state(client,db_session):
    first=await student(db_session)
    second=await student(db_session)
    _,org,_=await staff(db_session,'STUDIO')
    order=Order(organization_id=org.id,user_id=first.id,order_number=uuid.uuid4().hex,total_amount=10,status='PAID')
    db_session.add(order);await db_session.commit()
    payload={'order_id':str(order.id),'razorpay_order_id':'order_test','razorpay_payment_id':'pay_test','razorpay_signature':'x'}
    assert (await client.post('commerce/payments/verify',headers=auth(second),json=payload)).status_code==404
    response=await client.post('commerce/payments/verify',headers=auth(first),json=payload)
    assert response.status_code==409
    assert 'before paying again' in response.json()['detail']


@pytest.mark.asyncio
async def test_admission_selects_explicit_authorized_studio(client,db_session):
    from app.models.organization import OrganizationMember
    owner,first,_=await staff(db_session,'STUDIO')
    second=Organization(name='Second',slug=uuid.uuid4().hex)
    db_session.add(second);await db_session.flush()
    db_session.add(OrganizationMember(organization_id=second.id,user_id=owner.id,role='STUDIO'))
    await db_session.commit()
    user=await student(db_session)
    _,cls=await catalog(db_session,second)
    payload={'email':user.email,'class_id':str(cls.id),'studio_id':str(second.id)}
    assert (await client.post('students',headers=auth(owner),json=payload)).status_code==201
    assert await db_session.scalar(select(OrganizationStudent.id).where(OrganizationStudent.organization_id==second.id,OrganizationStudent.user_id==user.id))
    assert not await db_session.scalar(select(OrganizationStudent.id).where(OrganizationStudent.organization_id==first.id,OrganizationStudent.user_id==user.id))


@pytest.mark.asyncio
async def test_teacher_activation_contract_requires_confirmation_then_succeeds(client,db_session):
    owner,org,_=await staff(db_session,'STUDIO')
    teacher,_,member=await staff(db_session,'TEACHER',org)
    endpoint=f'teachers/{member.id}'
    assert (await client.put(endpoint,headers=auth(owner),json={'is_active':False})).status_code==403
    response=await client.put(endpoint,headers=auth(owner),json={'is_active':False,'studio_password':'strong-password'})
    assert response.status_code==200
    assert not response.json()['is_active']
