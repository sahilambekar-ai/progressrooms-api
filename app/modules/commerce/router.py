import uuid
from pydantic import BaseModel, model_validator
from fastapi import APIRouter, Depends, HTTPException, status, Request, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.commerce import Order, Payment, PaymentWebhookEvent, SessionEnrollment
from app.models.user import User
from app.modules.auth.dependencies import get_current_user
from app.modules.commerce.service import CommerceService, mock_payments
from app.common.enums import OrderStatus, PaymentStatus
from app.core.config import settings

router = APIRouter(prefix="/commerce", tags=["Commerce & Payments"])

class CreateOrderPayload(BaseModel):
    session_id: uuid.UUID | None = None
    tier_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def one_offering(self):
        if bool(self.session_id) == bool(self.tier_id):
            raise ValueError("Choose one class or pass")
        return self

class VerifyPaymentPayload(BaseModel):
    order_id: uuid.UUID
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

@router.post("/orders")
async def create_order(
    payload: CreateOrderPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if current_user.role != "STUDENT":
        raise HTTPException(403, "Please use a student account to purchase classes")
    try:
        order = await CommerceService.create_order(db, current_user.id, payload.session_id, payload.tier_id)
        return {
            "order_id": str(order.id),
            "order_number": order.order_number,
            "amount": float(order.total_amount),
            "currency": order.currency,
            "status": order.status,
            "razorpay_key_id": settings.RAZORPAY_KEY_ID,
            "razorpay_order_id": order.provider_order_id,
            "mock": mock_payments(),
            "free": order.total_amount == 0
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.post("/payments/verify")
async def verify_payment(
    payload: VerifyPaymentPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Order).where(Order.id == payload.order_id, Order.user_id == current_user.id).with_for_update()
    res = await db.execute(stmt)
    order = res.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if order.status == OrderStatus.PAID.value:
        enrollment = await db.scalar(select(SessionEnrollment).where(SessionEnrollment.order_id == order.id))
        return {"success": True, "enrollment_id": str(enrollment.id), "message": "Already activated"}
    if order.status != OrderStatus.PENDING.value or payload.razorpay_order_id != order.provider_order_id:
        raise HTTPException(400, "Payment does not match this order")
    if order.total_amount == 0:
        if payload.razorpay_payment_id != f"free_{order.id.hex}" or payload.razorpay_signature != "free-enrollment":
            raise HTTPException(400, "Invalid free enrollment confirmation")
    elif mock_payments():
        if payload.razorpay_payment_id != f"dev_{order.id.hex}" or payload.razorpay_signature != "development-only":
            raise HTTPException(400, "Invalid development payment")
    else:
        if not CommerceService.verify_razorpay_signature(order.provider_order_id, payload.razorpay_payment_id,
                                                        payload.razorpay_signature, settings.RAZORPAY_KEY_SECRET):
            raise HTTPException(400, "Invalid payment signature")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(f"https://api.razorpay.com/v1/payments/{payload.razorpay_payment_id}",
                                           auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
                response.raise_for_status()
                provider = response.json()
            from decimal import Decimal
            if (provider.get("order_id") != order.provider_order_id or provider.get("status") != "captured"
                or provider.get("amount") != int(Decimal(str(order.total_amount)) * 100)
                or provider.get("currency") != order.currency):
                raise HTTPException(400, "Payment has not been captured for this order yet")
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Could not confirm payment. Please retry verification.") from exc
    existing = await db.scalar(select(Payment.id).where(Payment.provider_payment_id == payload.razorpay_payment_id))
    if existing:
        raise HTTPException(400, "Payment has already been used")

    payment = Payment(
        order_id=order.id,
        amount=order.total_amount,
        currency=order.currency,
        payment_provider="FREE" if order.total_amount == 0 else ("MOCK" if mock_payments() else "RAZORPAY"),
        provider_order_id=payload.razorpay_order_id,
        provider_payment_id=payload.razorpay_payment_id,
        provider_signature=payload.razorpay_signature,
        status=PaymentStatus.SUCCESS.value,
        idempotency_key=payload.razorpay_payment_id
    )
    db.add(payment)
    order.status = OrderStatus.PAID.value

    enrollment = await CommerceService.activate_enrollment_from_order(db, order, payment)
    await db.commit()

    try:
        from app.modules.communication.billing_engine import notify_student_payment_received
        await notify_student_payment_received(db, order.id)
    except Exception as exc:
        import logging
        logging.getLogger("progressrooms.commerce").error(f"Failed to dispatch payment confirmation: {exc}")

    return {
        "success": True,
        "order_status": order.status,
        "enrollment_id": str(enrollment.id),
        "message": "Payment verified and enrollment activated"
    }

@router.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(None),
    db: AsyncSession = Depends(get_db)
):
    import hashlib, hmac, json
    raw = await request.body()
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret or secret.startswith("rzp_webhook_secret_mock"):
        raise HTTPException(503, "Payment webhooks are not configured")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not x_razorpay_signature or not hmac.compare_digest(expected, x_razorpay_signature):
        raise HTTPException(400, "Invalid webhook signature")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid webhook payload")
    entity = body.get("payload", {}).get("payment", {}).get("entity", {})
    event_type = body.get("event", "unknown")
    event_id = f"{event_type}:{entity.get('id', hashlib.sha256(raw).hexdigest())}"
    if await db.scalar(select(PaymentWebhookEvent.id).where(PaymentWebhookEvent.event_id == event_id)):
        return {"status": "already_processed"}
    if event_type == "payment.captured":
        from decimal import Decimal
        order = await db.scalar(select(Order).where(Order.provider_order_id == entity.get("order_id")).with_for_update())
        if not order:
            raise HTTPException(404, "Order not found")
        if (entity.get("status") != "captured" or entity.get("amount") != int(Decimal(str(order.total_amount)) * 100)
            or entity.get("currency") != order.currency):
            raise HTTPException(400, "Payment amount or currency does not match")
        if order.status == "PENDING":
            payment = Payment(order_id=order.id, amount=order.total_amount, currency=order.currency,
                payment_provider="RAZORPAY", provider_order_id=order.provider_order_id,
                provider_payment_id=entity["id"], status="SUCCESS", idempotency_key=entity["id"])
            db.add(payment)
            order.status = "PAID"
            await CommerceService.activate_enrollment_from_order(db, order, payment)
            await db.commit()

            try:
                from app.modules.communication.billing_engine import notify_student_payment_received
                await notify_student_payment_received(db, order.id)
            except Exception as exc:
                import logging
                logging.getLogger("progressrooms.commerce").error(f"Failed to dispatch payment confirmation: {exc}")
    db.add(PaymentWebhookEvent(event_id=event_id, event_type=event_type, payload=body, is_processed=True))
    await db.commit()
    return {"status": "received"}

