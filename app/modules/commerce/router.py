import uuid
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status, Request, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.commerce import Order, Payment, PaymentWebhookEvent
from app.models.user import User
from app.modules.auth.dependencies import get_current_user
from app.modules.commerce.service import CommerceService
from app.common.enums import OrderStatus, PaymentStatus
from app.core.config import settings

router = APIRouter(prefix="/commerce", tags=["Commerce & Payments"])

class CreateOrderPayload(BaseModel):
    session_id: uuid.UUID

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
    try:
        order = await CommerceService.create_order(db, current_user.id, payload.session_id)
        return {
            "order_id": str(order.id),
            "order_number": order.order_number,
            "amount": float(order.total_amount),
            "currency": order.currency,
            "status": order.status,
            "razorpay_key_id": settings.RAZORPAY_KEY_ID
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.post("/payments/verify")
async def verify_payment(
    payload: VerifyPaymentPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Order).where(Order.id == payload.order_id, Order.user_id == current_user.id)
    res = await db.execute(stmt)
    order = res.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    # In dev/mock mode or with actual key
    is_valid = True
    if settings.RAZORPAY_KEY_SECRET and not settings.RAZORPAY_KEY_SECRET.startswith("rzp_secret_mock"):
        is_valid = CommerceService.verify_razorpay_signature(
            payload.razorpay_order_id,
            payload.razorpay_payment_id,
            payload.razorpay_signature,
            settings.RAZORPAY_KEY_SECRET
        )

    if not is_valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payment signature")

    payment = Payment(
        order_id=order.id,
        amount=order.total_amount,
        currency=order.currency,
        payment_provider="RAZORPAY",
        provider_order_id=payload.razorpay_order_id,
        provider_payment_id=payload.razorpay_payment_id,
        provider_signature=payload.razorpay_signature,
        status=PaymentStatus.SUCCESS.value,
        idempotency_key=payload.razorpay_payment_id
    )
    db.add(payment)
    order.status = OrderStatus.PAID.value

    enrollment = await CommerceService.activate_enrollment_from_order(db, order, payment)

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
    body = await request.json()
    event_id = body.get("event_id") or body.get("payload", {}).get("payment", {}).get("entity", {}).get("id") or str(uuid.uuid4())
    event_type = body.get("event", "unknown")

    # Idempotency check
    existing = await db.execute(select(PaymentWebhookEvent).where(PaymentWebhookEvent.event_id == event_id))
    if existing.scalar_one_or_none():
        return {"status": "already_processed"}

    evt = PaymentWebhookEvent(
        event_id=event_id,
        event_type=event_type,
        payload=body,
        is_processed=True
    )
    db.add(evt)
    await db.commit()
    return {"status": "received"}
