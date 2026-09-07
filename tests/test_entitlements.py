import pytest
from datetime import datetime, timedelta, timezone
from app.models.organization import Organization
from app.models.plan import Plan, PlanFeature, OrganizationSubscription
from app.modules.plans.entitlement_service import FeatureEntitlementService

@pytest.mark.asyncio
async def test_feature_entitlements(db_session):
    now = datetime.now(timezone.utc)

    # 1. Create Starter Plan (advanced_website = false)
    plan_starter = Plan(name="Starter", slug="starter-test", price=0.00)
    db_session.add(plan_starter)
    await db_session.flush()

    db_session.add(PlanFeature(plan_id=plan_starter.id, feature_key="advanced_website", feature_value="false"))
    db_session.add(PlanFeature(plan_id=plan_starter.id, feature_key="max_teachers", feature_value="1"))

    # 2. Create Studio Plan (advanced_website = true)
    plan_studio = Plan(name="Studio", slug="studio-test", price=3999.00)
    db_session.add(plan_studio)
    await db_session.flush()

    db_session.add(PlanFeature(plan_id=plan_studio.id, feature_key="advanced_website", feature_value="true"))
    db_session.add(PlanFeature(plan_id=plan_studio.id, feature_key="max_teachers", feature_value="10"))

    # 3. Create Org on Starter
    org1 = Organization(name="Solo Yoga", slug="solo-yoga")
    db_session.add(org1)
    await db_session.flush()

    db_session.add(OrganizationSubscription(
        organization_id=org1.id,
        plan_id=plan_starter.id,
        status="ACTIVE",
        starts_at=now,
        ends_at=now + timedelta(days=30)
    ))

    # 4. Create Org on Studio
    org2 = Organization(name="Big Studio", slug="big-studio")
    db_session.add(org2)
    await db_session.flush()

    db_session.add(OrganizationSubscription(
        organization_id=org2.id,
        plan_id=plan_studio.id,
        status="ACTIVE",
        starts_at=now,
        ends_at=now + timedelta(days=30)
    ))

    await db_session.commit()

    # Verify entitlements
    can_starter_website = await FeatureEntitlementService.can_use_feature(db_session, org1.id, "advanced_website")
    assert can_starter_website is False

    can_studio_website = await FeatureEntitlementService.can_use_feature(db_session, org2.id, "advanced_website")
    assert can_studio_website is True

    # Check usage limit for teachers
    within_limit_org1 = await FeatureEntitlementService.check_usage_limit(db_session, org1.id, "max_teachers", current_count=1)
    assert within_limit_org1 is False # limit is 1, current_count 1 reaches limit

    within_limit_org2 = await FeatureEntitlementService.check_usage_limit(db_session, org2.id, "max_teachers", current_count=5)
    assert within_limit_org2 is True # limit is 10, current_count 5 is ok
