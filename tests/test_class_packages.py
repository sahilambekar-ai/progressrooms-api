"""Package selection, price snapshots and per-class pass allowances."""
import uuid
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from app.models.session import SessionPricing, SessionInstructor
from app.models.commerce import OrderItem, SessionEnrollment
from app.models.organization import OrganizationMember
from app.models.user import User
from app.modules.classes.schemas import ClassPackage, SessionPricingCreate
from app.modules.commerce.service import available_credit
from test_storefront import store, auth


def package(count=7, price=700, offer=500):
    return {'id': str(uuid.uuid4()), 'total_classes': count, 'price': price,
            'offer_price': offer, 'validity_days': 45}


def test_package_validation():
    for override in [{'total_classes': 0}, {'total_classes': 1.5}, {'validity_days': 0},
                     {'price': -1}, {'offer_price': 701}, {'price': float('nan')}]:
        with pytest.raises(ValidationError):
            ClassPackage(**{**package(), **override})
    assert ClassPackage(**package(offer=0)).model_dump(mode='json')['offer_price'] == 0
    p = package()
    with pytest.raises(ValidationError):
        SessionPricingCreate(price=0, packages=[p, p])
    with pytest.raises(ValidationError):
        SessionPricingCreate(price=0, packages=[])


async def set_packages(db, session, options):
    pricing = await db.scalar(select(SessionPricing).where(SessionPricing.session_id == session.id))
    pricing.packages = options
    await db.commit()
    return pricing


async def verify(client, user, order):
    response = await client.post('/commerce/payments/verify', headers=auth(user), json={
        'order_id': order['order_id'], 'razorpay_order_id': order['razorpay_order_id'],
        'razorpay_payment_id': f"dev_{order['order_id'].replace('-', '')}",
        'razorpay_signature': 'development-only'})
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_class_package_buy_and_frozen_allowance(store, db_session):
    client, student, org, classes, _ = store
    options = [package(1, 100, None), package()]
    pricing = await set_packages(db_session, classes[0], options)
    public = (await client.get(f'/public/organizations/{org.slug}')).json()
    assert 'refunds_supported' not in public['purchase_policy']
    assert 'refund' not in public['purchase_policy']['terms'].lower()
    assert public['sessions'][0]['pricing']['packages'] == options
    detail = (await client.get(f'/public/organizations/{org.slug}/sessions/{classes[0].slug}'))
    # Both public catalog and detail must expose all purchase options.
    assert detail.status_code == 200, detail.text
    assert detail.json()['pricing']['packages'] == options
    for invalid in [{}, {'package_id': str(uuid.uuid4())}]:
        response = await client.post('/commerce/orders', headers=auth(student), json={'session_id': str(classes[0].id), **invalid})
        assert response.status_code == 400
    response = await client.post('/commerce/orders', headers=auth(student), json={
        'session_id': str(classes[0].id), 'package_id': options[1]['id'], 'price': 1})
    assert response.status_code == 200, response.text
    order = response.json()
    assert order['amount'] == 500
    pricing.packages = [package(30, 3000, None)]
    await db_session.commit()
    await verify(client, student, order)
    enrollment = await db_session.scalar(select(SessionEnrollment).where(SessionEnrollment.user_id == student.id))
    assert enrollment.total_sessions == 7
    assert (enrollment.valid_until - enrollment.valid_from).days == 45
    assert enrollment.billing_value == 500


async def staff(db, org, role):
    user = User(email=f'{uuid.uuid4()}@example.com', full_name=role, role=role,
                is_verified=True, account_status='ACTIVE')
    db.add(user); await db.flush()
    member = OrganizationMember(organization_id=org.id, user_id=user.id, role=role, is_active=True)
    db.add(member); await db.commit()
    return user, member


@pytest.mark.asyncio
async def test_membership_child_packages_and_independent_credits(store, db_session):
    client, student, org, classes, _ = store
    owner, _ = await staff(db_session, org, 'STUDIO')
    options = [package(1, 100, None), package(7)]
    await set_packages(db_session, classes[0], options)
    other = package(3, 300, None)
    await set_packages(db_session, classes[1], [other])
    selections = [{'session_id': str(classes[0].id), 'package_id': p['id']} for p in options]
    selections.append({'session_id': str(classes[1].id), 'package_id': other['id']})
    response = await client.post(f'/organizations/{org.id}/membership-tiers', headers=auth(owner), json={
        'name': 'Practice together', 'price': 999, 'offer_price': 899,
        'class_packages': selections, 'total_classes': 500})
    assert response.status_code == 201, response.text
    tier = response.json()
    assert tier['total_classes'] == 11
    assert len(tier['attached_class_ids']) == 2
    assert len(tier['class_packages']) == 3
    # Package edits after pass creation must not change its promised class counts.
    await set_packages(db_session, classes[0], [package(30, 3000, None)])
    response = await client.post('/commerce/orders', headers=auth(student), json={'tier_id': tier['id']})
    assert response.status_code == 200, response.text
    order = response.json(); assert order['amount'] == 899
    items = (await db_session.execute(select(OrderItem).where(OrderItem.order_id == uuid.UUID(order['order_id'])))).scalars().all()
    assert sorted(i.total_classes for i in items) == [3, 8]
    await verify(client, student, order)
    enrollments = (await db_session.execute(select(SessionEnrollment).where(SessionEnrollment.user_id == student.id))).scalars().all()
    first = next(e for e in enrollments if e.session_id == classes[0].id)
    second = next(e for e in enrollments if e.session_id == classes[1].id)
    first.sessions_attended = 8
    await db_session.commit()
    assert not await available_credit(db_session, first)
    assert await available_credit(db_session, second)
    dashboard = (await client.get('/student/enrollments', headers=auth(student))).json()
    assert all(e['total_sessions'] == 11 and e['sessions_attended'] == 8 for e in dashboard)
    listed = (await client.get(f'/organizations/{org.id}/membership-tiers', headers=auth(owner))).json()
    assert next(t for t in listed if t['id'] == tier['id'])['class_packages'] == tier['class_packages']


@pytest.mark.asyncio
async def test_membership_rejects_foreign_or_duplicate_packages(store, db_session):
    client, _, org, classes, _ = store
    owner, _ = await staff(db_session, org, 'STUDIO')
    p = package()
    await set_packages(db_session, classes[0], [p])
    selected = {'session_id': str(classes[0].id), 'package_id': p['id']}
    for selections in [[selected, selected], [{**selected, 'package_id': str(uuid.uuid4())}],
                       [{**selected, 'session_id': str(uuid.uuid4())}]]:
        response = await client.post(f'/organizations/{org.id}/membership-tiers', headers=auth(owner), json={
            'name': 'Invalid', 'price': 100, 'class_packages': selections})
        assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_teacher_can_edit_only_assigned_class_pricing(store, db_session):
    client, _, org, classes, _ = store
    teacher, member = await staff(db_session, org, 'TEACHER')
    db_session.add(SessionInstructor(session_id=classes[0].id, member_id=member.id, role='PRIMARY'))
    await db_session.commit()
    payload = {'pricing': {'price': 100, 'pricing_type': 'PACKAGE', 'packages': [package()]}}
    url = f'/organizations/{org.id}/classes'
    response = await client.put(f'{url}/{classes[0].id}', headers=auth(teacher), json=payload)
    assert response.status_code == 200, response.text
    assert (await client.put(f'{url}/{classes[1].id}', headers=auth(teacher), json=payload)).status_code == 403
    assert (await client.put(f'{url}/{classes[0].id}', headers=auth(teacher), json={**payload, 'capacity': 999})).status_code == 403
    catalog = (await client.get(url, headers=auth(teacher))).json()
    assert len(catalog) == 1 and catalog[0]['pricing']['packages'][0]['total_classes'] == 7

@pytest.mark.asyncio
async def test_pass_edit_preserves_removed_package_snapshot(store, db_session):
    client, _, org, classes, _ = store
    owner, _ = await staff(db_session, org, 'STUDIO')
    option = package()
    await set_packages(db_session, classes[0], [option])
    selection = {'session_id': str(classes[0].id), 'package_id': option['id']}
    url = f'/organizations/{org.id}/membership-tiers'
    response = await client.post(url, headers=auth(owner), json={
        'name': 'Saved package', 'price': 800, 'class_packages': [selection]})
    assert response.status_code == 201, response.text
    tier = response.json()
    await set_packages(db_session, classes[0], [package(30, 3000, None)])
    response = await client.put(f"{url}/{tier['id']}", headers=auth(owner), json={
        'price': 900, 'class_packages': [selection], 'total_classes': 999})
    assert response.status_code == 200, response.text
    assert response.json()['total_classes'] == 7
    assert response.json()['class_packages'] == tier['class_packages']


@pytest.mark.asyncio
async def test_package_can_be_purchased_after_credits_are_used(store, db_session):
    client, student, _, classes, _ = store
    option = package()
    await set_packages(db_session, classes[0], [option])
    body = {'session_id': str(classes[0].id), 'package_id': option['id']}
    response = await client.post('/commerce/orders', headers=auth(student), json=body)
    assert response.status_code == 200, response.text
    await verify(client, student, response.json())
    assert (await client.post('/commerce/orders', headers=auth(student), json=body)).status_code == 400
    enrollment = await db_session.scalar(select(SessionEnrollment).where(SessionEnrollment.user_id == student.id))
    enrollment.sessions_attended = 7
    await db_session.commit()
    assert (await client.post('/commerce/orders', headers=auth(student), json=body)).status_code == 200

@pytest.mark.asyncio
async def test_create_group_class_with_package_options_and_schedule(store, db_session):
    from datetime import date
    from app.models.schedule import ClassOccurrence
    client, _, org, classes, _ = store
    owner, member = await staff(db_session, org, 'STUDIO')
    options = [package(1, 100, None), package(7), package(30, 3000, 2500)]
    response = await client.post(f'/organizations/{org.id}/classes', headers=auth(owner), json={
        'class_type_id': str(classes[0].class_type_id), 'name': 'Packaged group class',
        'slug': f'packages-{uuid.uuid4().hex}', 'session_type': 'GROUP', 'is_active': False,
        'instructor_member_ids': [str(member.id)],
        'pricing': {'pricing_type': 'PACKAGE', 'price': 100, 'packages': options},
        'schedule_rules': [{'day_of_week': date.today().weekday(), 'start_time': '21:00',
                            'end_time': '22:00', 'effective_start_date': date.today().isoformat()}]})
    assert response.status_code == 201, response.text
    created_id = uuid.UUID(response.json()['id'])
    pricing = await db_session.scalar(select(SessionPricing).where(SessionPricing.session_id == created_id))
    assert pricing.packages == options
    assert await db_session.scalar(select(ClassOccurrence.id).where(ClassOccurrence.session_id == created_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_update_class_requirements_without_lazy_loading(store, db_session, existing):
    from app.models.session import SessionRequirement
    client, _, org, classes, _ = store
    owner, _ = await staff(db_session, org, 'STUDIO')
    session = classes[0]
    if existing:
        db_session.add(SessionRequirement(session_id=session.id, prerequisites="Old instructions"))
        await db_session.commit()
    # Match a fresh request where this relationship has not been loaded.
    db_session.expire(session, ["requirements"])
    requirements = {
        "prerequisites": "Some experience",
        "required_equipment": "Yoga mat",
        "preparation_instructions": "Arrive early",
        "additional_notes": "Bring water",
    }
    response = await client.put(
        f'/organizations/{org.id}/classes/{session.id}',
        headers=auth(owner), json={"requirements": requirements},
    )
    assert response.status_code == 200, response.text
    saved = await db_session.scalar(select(SessionRequirement).where(SessionRequirement.session_id == session.id))
    assert saved is not None
    for key, value in requirements.items():
        assert getattr(saved, key) == value
