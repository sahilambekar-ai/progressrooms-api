import uuid
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.session import Session, SessionPricing
from app.models.commerce import Order, OrderItem, Payment, PaymentWebhookEvent, SessionEnrollment
from app.models.user import User
from app.common.enums import OrderStatus, PaymentStatus, EnrollmentStatus, PricingType
from app.core.config import settings

class CommerceService:
    @staticmethod
    async def create_order(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> Order:
        # Fetch session and pricing
        stmt = select(Session).options(selectinload(Session.pricing)).where(
            Session.id == session_id,
            Session.is_active == True,
            Session.deleted_at.is_(None)
        )
        res = await db.execute(stmt)
        session = res.scalar_one_or_none()
        if not session or not session.pricing:
            raise ValueError("Session or pricing not found")

        order_number = f"PR-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
        order = Order(
            organization_id=session.organization_id,
            user_id=user_id,
            order_number=order_number,
            total_amount=session.pricing.price,
            currency=session.pricing.currency,
            status=OrderStatus.PENDING.value
        )
        db.add(order)
        await db.flush()

        item = OrderItem(
            order_id=order.id,
            session_id=session.id,
            unit_price=session.pricing.price,
            quantity=1,
            subtotal=session.pricing.price
        )
        db.add(item)
        await db.commit()
        await db.refresh(order)
        return order

    @staticmethod
    def verify_razorpay_signature(razorpay_order_id: str, razorpay_payment_id: str, signature: str, secret: str) -> bool:
        """Verifies HMAC SHA256 signature from Razorpay."""
        msg = f"{razorpay_order_id}|{razorpay_payment_id}".encode("utf-8")
        expected_sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected_sig, signature)

    @staticmethod
    async def activate_enrollment_from_order(db: AsyncSession, order: Order, payment: Payment) -> SessionEnrollment:
        """Activates student session enrollment upon verified payment."""
        stmt = select(OrderItem).options(
            selectinload(OrderItem.session).selectinload(Session.pricing)
        ).where(OrderItem.order_id == order.id)
        res = await db.execute(stmt)
        item = res.scalars().first()
        session = item.session
        pricing = session.pricing

        now = datetime.now(timezone.utc)
        valid_until = None
        total_sessions = None

        if pricing.pricing_type == PricingType.MONTHLY.value:
            valid_until = now + timedelta(days=pricing.validity_days or 30)
        elif pricing.pricing_type == PricingType.PACKAGE.value:
            total_sessions = pricing.total_classes or 10
            valid_until = now + timedelta(days=pricing.validity_days or 60)
        elif pricing.pricing_type == PricingType.ONE_TIME.value:
            valid_until = now + timedelta(days=pricing.validity_days or 30)

        enrollment = SessionEnrollment(
            organization_id=order.organization_id,
            session_id=session.id,
            user_id=order.user_id,
            order_id=order.id,
            status=EnrollmentStatus.ACTIVE.value,
            valid_from=now,
            valid_until=valid_until,
            total_sessions=total_sessions,
            sessions_attended=0
        )
        db.add(enrollment)
        await db.commit()
        await db.refresh(enrollment)
        return enrollment
