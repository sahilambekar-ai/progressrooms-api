import uuid
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.organization import Organization
from app.models.plan import Plan, PlanFeature, OrganizationSubscription
from app.common.enums import SubscriptionStatus
from app.common.dates import ensure_utc

class FeatureEntitlementService:
    @staticmethod
    async def can_use_feature(db: AsyncSession, organization_id: uuid.UUID, feature_key: str) -> bool:
        """Centralized entitlement check for plan gating."""
        org_stmt = select(Organization).where(Organization.id == organization_id, Organization.deleted_at.is_(None))
        org_res = await db.execute(org_stmt)
        org = org_res.scalar_one_or_none()
        if not org or org.status != "ACTIVE":
            return False

        sub_stmt = select(OrganizationSubscription).where(
            OrganizationSubscription.organization_id == organization_id
        )
        sub_res = await db.execute(sub_stmt)
        subscription = sub_res.scalar_one_or_none()
        if not subscription:
            return False

        now = datetime.now(timezone.utc)
        trial_ends_at = ensure_utc(subscription.trial_ends_at)
        ends_at = ensure_utc(subscription.ends_at)

        if subscription.status == SubscriptionStatus.TRIAL.value:
            if trial_ends_at and now > trial_ends_at:
                return False
        elif subscription.status == SubscriptionStatus.ACTIVE.value:
            if ends_at and now > ends_at:
                return False
        else:
            return False

        feat_stmt = select(PlanFeature).where(
            PlanFeature.plan_id == subscription.plan_id,
            PlanFeature.feature_key == feature_key
        )
        feat_res = await db.execute(feat_stmt)
        feature = feat_res.scalar_one_or_none()
        if not feature:
            return False

        val = feature.feature_value.strip().lower()
        if val in ("true", "1", "yes"):
            return True
        elif val in ("false", "0", "no"):
            return False

        try:
            limit_val = int(val)
            return limit_val != 0
        except ValueError:
            return True

    @staticmethod
    async def check_usage_limit(db: AsyncSession, organization_id: uuid.UUID, feature_key: str, current_count: int) -> bool:
        sub_stmt = select(OrganizationSubscription).where(
            OrganizationSubscription.organization_id == organization_id
        )
        sub_res = await db.execute(sub_stmt)
        subscription = sub_res.scalar_one_or_none()
        if not subscription:
            return False

        feat_stmt = select(PlanFeature).where(
            PlanFeature.plan_id == subscription.plan_id,
            PlanFeature.feature_key == feature_key
        )
        feat_res = await db.execute(feat_stmt)
        feature = feat_res.scalar_one_or_none()
        if not feature:
            return False

        try:
            limit = int(feature.feature_value)
            if limit == -1:
                return True
            return current_count < limit
        except ValueError:
            return True
