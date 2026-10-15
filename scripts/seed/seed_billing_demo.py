"""Idempotently add subscription billing history for the demo studio."""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.commerce import SessionEnrollment
from app.models.organization import Organization
from app.models.session import Session, SessionPricing
from app.models.user import User


async def seed_billing_demo() -> None:
    async with AsyncSessionLocal() as db:
        org = await db.scalar(select(Organization).where(Organization.slug == "demo-yoga-studio"))
        if not org:
            raise RuntimeError("Run scripts.seed.seed_data first; demo-yoga-studio was not found")

        class_rows = (await db.execute(
            select(Session, SessionPricing)
            .join(SessionPricing, SessionPricing.session_id == Session.id)
            .where(Session.organization_id == org.id)
            .order_by(Session.name)
        )).all()
        students = (await db.scalars(
            select(User).where(User.email.like("student%@test.progressrooms.local")).order_by(User.email)
        )).all()
        if not class_rows or not students:
            raise RuntimeError("Demo classes or students are missing; run the base seed first")

        # Backfill snapshots for older demo enrollments so price edits cannot rewrite reports.
        pricing_by_session = {session.id: (session, pricing) for session, pricing in class_rows}
        existing_enrollments = (await db.scalars(
            select(SessionEnrollment).where(SessionEnrollment.organization_id == org.id)
        )).all()
        backfilled = 0
        for enrollment in existing_enrollments:
            pair = pricing_by_session.get(enrollment.session_id)
            if pair and enrollment.billing_value is None:
                session, pricing = pair
                enrollment.billing_value = pricing.price
                enrollment.billing_currency = pricing.currency
                enrollment.billing_type = pricing.pricing_type
                enrollment.billing_label = session.name
                backfilled += 1

        now = datetime.now(timezone.utc)
        offsets = [0, 2, 5, 9, 14, 21, 29, 36, 48, 63, 78, 96, 124, 158, 205, 260, 320]
        created = 0
        for index, days_ago in enumerate(offsets):
            session, pricing = class_rows[index % len(class_rows)]
            student = students[index % len(students)]
            billing_date = (now - timedelta(days=days_ago)).replace(hour=12, minute=0, second=0, microsecond=0)
            exists = await db.scalar(select(SessionEnrollment.id).where(
                SessionEnrollment.organization_id == org.id,
                SessionEnrollment.session_id == session.id,
                SessionEnrollment.user_id == student.id,
                SessionEnrollment.valid_from == billing_date,
            ))
            if exists:
                continue
            db.add(SessionEnrollment(
                organization_id=org.id,
                session_id=session.id,
                user_id=student.id,
                status="ACTIVE" if index % 5 else "EXPIRED",
                valid_from=billing_date,
                valid_until=billing_date + timedelta(days=pricing.validity_days or 30),
                total_sessions=pricing.total_classes,
                sessions_attended=min(index % 8, pricing.total_classes or 99),
                billing_value=pricing.price,
                billing_currency=pricing.currency,
                billing_type=pricing.pricing_type,
                billing_label=session.name,
            ))
            created += 1

        await db.commit()
        print(f"Billing demo ready: {created} new rows, {backfilled} existing rows backfilled")


if __name__ == "__main__":
    asyncio.run(seed_billing_demo())
