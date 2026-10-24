import uuid
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select
from app.models.session import SessionPricing
from app.models.schedule import ClassOccurrence
from app.models.commerce import SessionEnrollment
from app.modules.classes.schemas import SessionCreate
from app.modules.classes.service import auto_complete_expired_one_on_one_classes
from test_storefront import store, auth
from test_class_packages import package, set_packages, verify


def test_unscheduled_class_schema_accepts_unlimited_capacity():
    value = SessionCreate(class_type_id=uuid.uuid4(), name='Flexible lessons', slug='flexible', session_type='ONE_TO_ONE', capacity=None,
                          full_description='<p>Seven <strong>private lessons</strong></p>', pricing={'price': 700, 'pricing_type': 'PACKAGE', 'total_classes': 7, 'validity_days':70})
    assert value.capacity is None and value.schedule_rules == []


@pytest.mark.asyncio
async def test_unscheduled_package_purchase_starts_validity_at_payment(store, db_session):
    client, student, org, classes, _ = store
    lesson = classes[0]
    lesson.capacity = None
    lesson.session_type = 'ONE_TO_ONE'
    lesson.full_description = '<p>Arrange each lesson with your teacher.</p>'
    option = {**package(), 'validity_days':70}
    await set_packages(db_session, lesson, [option])
    public = (await client.get(f'/public/organizations/{org.slug}')).json()
    item = next(s for s in public['sessions'] if s['id'] == str(lesson.id))
    assert item['capacity'] is None and item['remaining_spots'] is None
    assert item['schedule'] == [] and item['full_description'] == lesson.full_description
    before = datetime.now(timezone.utc)
    response = await client.post('/commerce/orders', headers=auth(student), json={'session_id':str(lesson.id),'package_id':option['id']})
    assert response.status_code == 200, response.text
    await verify(client, student, response.json())
    enrollment = await db_session.scalar(select(SessionEnrollment).where(SessionEnrollment.session_id==lesson.id))
    assert enrollment.total_sessions == 7
    assert enrollment.valid_from.replace(tzinfo=timezone.utc) >= before
    assert enrollment.valid_until - enrollment.valid_from == timedelta(days=70)
    assert not (await db_session.scalars(select(ClassOccurrence).where(ClassOccurrence.session_id==lesson.id))).all()


@pytest.mark.asyncio
async def test_expired_appointment_archives_but_reusable_package_stays(store, db_session):
    _, _, org, classes, _ = store
    past = datetime.now(timezone.utc)-timedelta(days=1)
    for index, lesson in enumerate(classes):
        lesson.session_type='ONE_TO_ONE'
        pricing=await db_session.scalar(select(SessionPricing).where(SessionPricing.session_id==lesson.id))
        pricing.pricing_type='PER_SESSION' if index==0 else 'PACKAGE'
        pricing.total_classes=1 if index==0 else 7
        db_session.add(ClassOccurrence(organization_id=org.id, session_id=lesson.id, scheduled_start_at=past,
            scheduled_end_at=past+timedelta(hours=1),actual_start_at=past,actual_end_at=past+timedelta(hours=1),status='CANCELLED'))
    await db_session.commit()
    await auto_complete_expired_one_on_one_classes(db_session, org.id)
    await db_session.refresh(classes[0]); await db_session.refresh(classes[1])
    assert classes[0].deleted_at is not None and not classes[0].is_active
    assert classes[1].deleted_at is None and classes[1].is_active
