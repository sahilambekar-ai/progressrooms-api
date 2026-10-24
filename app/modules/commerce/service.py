import uuid
import hmac
import hashlib
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import httpx
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.session import Session
from app.models.organization import Organization, OrganizationStudent
from app.models.membership_tier import MembershipTier, MembershipTierClass
from app.models.commerce import Order, OrderItem, Payment, SessionEnrollment
from app.core.config import settings


def mock_payments():
    # Mock checkout is deliberately opt-in and cannot be enabled by a request.
    return settings.DEV_TEST_LOGIN_ENABLED and settings.RAZORPAY_KEY_SECRET.startswith('rzp_secret_mock')


class CommerceService:
    @staticmethod
    async def create_order(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID | None = None,
                           tier_id: uuid.UUID | None = None, package_id: uuid.UUID | None = None) -> Order:
        if bool(session_id) == bool(tier_id):
            raise ValueError('Choose one class or pass')
        package = None
        class_limits = {}
        if package_id and not session_id:
            raise ValueError("A package must belong to a class")
        if tier_id:
            offering = await db.scalar(select(MembershipTier).join(Organization).options(
                selectinload(MembershipTier.classes).selectinload(MembershipTierClass.session)
            ).where(MembershipTier.id == tier_id, MembershipTier.is_active == True,
                    MembershipTier.deleted_at.is_(None), Organization.status == 'ACTIVE', Organization.deleted_at.is_(None)))
            if not offering:
                raise ValueError('This pass is no longer available')
            sessions = [c.session for c in offering.classes if c.session.is_active and c.session.is_public
                        and c.session.deleted_at is None and c.session.organization_id == offering.organization_id]
            if offering.class_packages:
                selected_ids = {p["session_id"] for p in offering.class_packages}
                if selected_ids != {str(s.id) for s in sessions}:
                    raise ValueError("One or more included classes are unavailable. Please contact the studio.")
                for p in offering.class_packages:
                    class_limits[p["session_id"]] = class_limits.get(p["session_id"], 0) + p["total_classes"]
            pricing = offering
            kind = offering.tier_type
            existing_pass = await db.scalar(
                select(SessionEnrollment.id).join(Order, SessionEnrollment.purchase_order_id == Order.id).where(
                    SessionEnrollment.user_id == user_id,
                    SessionEnrollment.status == "ACTIVE",
                    Order.offering_label == offering.name
                )
            )
            if existing_pass:
                raise ValueError("You already have active access to this pass. You can use your remaining credits on your dashboard.")
        else:
            offering = await db.scalar(select(Session).join(Organization).options(selectinload(Session.pricing)).where(
                Session.id == session_id, Session.is_active == True, Session.is_public == True,
                Session.deleted_at.is_(None), Organization.status == 'ACTIVE', Organization.deleted_at.is_(None)))
            if not offering or not offering.pricing:
                raise ValueError('This class is no longer available')
            sessions, pricing, kind = [offering], offering.pricing, offering.pricing.pricing_type
            if pricing.packages:
                if not package_id:
                    raise ValueError("Choose a class package")
                package = next((p for p in pricing.packages if p["id"] == str(package_id)), None)
                if not package:
                    raise ValueError("This class package is no longer available")
                kind = "PACKAGE"
            elif package_id:
                raise ValueError("This class package is no longer available")
            existing_enrollments = (await db.execute(
                select(SessionEnrollment).where(
                    SessionEnrollment.user_id == user_id,
                    SessionEnrollment.session_id == session_id,
                    SessionEnrollment.status == "ACTIVE",
                    (SessionEnrollment.valid_until.is_(None) | (SessionEnrollment.valid_until > datetime.now(timezone.utc)))
                )
            )).scalars().all()
            for enrollment in existing_enrollments:
                if await available_credit(db, enrollment):
                    raise ValueError("You already have active access to this class. You can find it under 'Upcoming classes' on your dashboard.")
        if not sessions:
            raise ValueError('This pass does not have any available classes yet')
        from app.modules.billing.saas import require_operational_access
        await require_operational_access(db, offering.organization_id)
        for s in sessions:
            active_enrolled = await db.scalar(
                select(func.count(SessionEnrollment.id)).where(
                    SessionEnrollment.session_id == s.id,
                    SessionEnrollment.status == "ACTIVE"
                )
            ) or 0
            if s.capacity is not None and active_enrolled >= s.capacity:
                raise ValueError(f"'{s.name}' is at full capacity ({s.capacity} spots). No seats remaining.")
        if pricing.total_classes is not None and pricing.total_classes < 1:
            raise ValueError('The studio needs to correct the session allowance')
        if pricing.validity_days is not None and pricing.validity_days < 1:
            raise ValueError('The studio needs to correct the validity period')
        amount_due = offering.offer_price if tier_id and offering.offer_price is not None else pricing.price
        if package:
            amount_due = Decimal(str(package["offer_price"] if package.get("offer_price") is not None else package["price"]))
        credits = package["total_classes"] if package else pricing.total_classes
        days = package["validity_days"] if package else pricing.validity_days
        if class_limits:
            credits = sum(class_limits.values())
        if amount_due > 0 and not mock_payments() and settings.RAZORPAY_KEY_SECRET.startswith('rzp_secret_mock'):
            raise ValueError('Online payments are not configured yet. Please contact the studio.')
        order = Order(organization_id=offering.organization_id, user_id=user_id,
                      order_number=f"PR-{uuid.uuid4().hex[:16].upper()}", total_amount=amount_due,
                      currency=pricing.currency, status='PENDING', offering_label=f"{offering.name} · {credits} sessions" if package else offering.name,
                      offering_type=kind, credit_limit=credits if credits is not None else (1 if kind in ("ONE_TIME", "PER_SESSION", "DROP_IN") else None),
                      validity_days=days or 30)
        db.add(order)
        await db.flush()
        # Freeze eligible classes now; later catalog edits cannot change this purchase.
        for index, session in enumerate(sessions):
            amount = amount_due if index == 0 else Decimal('0')
            db.add(OrderItem(order_id=order.id, session_id=session.id, unit_price=amount, quantity=1, subtotal=amount,
                total_classes=class_limits.get(str(session.id))))
        if order.total_amount == 0:
            order.provider_order_id = f"free_{order.id.hex}"
        elif mock_payments():
            order.provider_order_id = f'mock_{order.id.hex}'
        else:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post('https://api.razorpay.com/v1/orders',
                        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
                        json={'amount': int(Decimal(str(order.total_amount)) * 100), 'currency': order.currency,
                              'receipt': order.order_number})
                    response.raise_for_status()
                    order.provider_order_id = response.json()['id']
            except (httpx.HTTPError, KeyError) as exc:
                await db.rollback()
                raise ValueError('Could not start payment. Please try again.') from exc
        await db.commit()
        return order

    @staticmethod
    def verify_razorpay_signature(razorpay_order_id, razorpay_payment_id, signature, secret):
        expected = hmac.new(secret.encode(), f'{razorpay_order_id}|{razorpay_payment_id}'.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    @staticmethod
    async def activate_enrollment_from_order(db, order, payment):
        items = (await db.execute(select(OrderItem).options(selectinload(OrderItem.session).selectinload(Session.pricing))
                                  .where(OrderItem.order_id == order.id).order_by(OrderItem.created_at, OrderItem.id))).scalars().all()
        now = datetime.now(timezone.utc)
        first = None
        for index, item in enumerate(items):
            pricing = item.session.pricing
            credits = item.total_classes if item.total_classes is not None else (order.credit_limit if order.offering_label else pricing.total_classes)
            days = order.validity_days if order.offering_label else pricing.validity_days or 30
            enrollment = SessionEnrollment(organization_id=order.organization_id, session_id=item.session_id,
                user_id=order.user_id, order_id=order.id if index == 0 else None, purchase_order_id=order.id,
                status='ACTIVE', valid_from=now, valid_until=now + timedelta(days=days),
                total_sessions=credits, sessions_attended=0,
                billing_value=order.total_amount if index == 0 else 0, billing_currency=order.currency,
                billing_type=order.offering_type or pricing.pricing_type,
                billing_label=order.offering_label or item.session.name)
            db.add(enrollment)
            first = first or enrollment
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        insert = sqlite_insert if db.bind.dialect.name == 'sqlite' else pg_insert
        await db.execute(insert(OrganizationStudent).values(organization_id=order.organization_id,
            user_id=order.user_id, status='ACTIVE').on_conflict_do_nothing(index_elements=['organization_id', 'user_id']))
        await db.flush()
        return first


async def available_credit(db, enrollment, *, lock_pass=True):
    if enrollment.total_sessions is None:
        return True
    used = enrollment.sessions_attended
    if enrollment.purchase_order_id:
        limit = await db.scalar(select(OrderItem.total_classes).where(
            OrderItem.order_id == enrollment.purchase_order_id, OrderItem.session_id == enrollment.session_id))
        if limit is not None:
            return used < limit
        # Serialize attendance mutations on the pass to protect its shared allowance.
        if lock_pass:
            await db.scalar(select(Order).where(Order.id == enrollment.purchase_order_id).with_for_update())
        used = await db.scalar(select(func.sum(SessionEnrollment.sessions_attended)).where(
            SessionEnrollment.purchase_order_id == enrollment.purchase_order_id)) or 0
    return used < enrollment.total_sessions
