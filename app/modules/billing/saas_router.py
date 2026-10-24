import hashlib
import hmac
import json
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.common.enums import UserRole
from app.core.config import settings
from app.core.database import get_db
from app.models.plan import Plan, OrganizationSubscription
from app.models.saas_billing import SaaSCheckout, SaaSWebhookEvent
from app.models.user import User
from app.modules.auth.dependencies import get_current_user
from app.modules.organizations.dependencies import get_current_tenant_member, require_org_role
from . import saas

router = APIRouter(tags=["Progress Rooms SaaS Billing"])
owner = require_org_role([UserRole.STUDIO])
prefix = "/organizations/{org_id}/saas-billing"


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: uuid.UUID
    billing_country: str = Field(pattern=r"^[A-Z]{2}$")


class CheckoutRequest(QuoteRequest):
    idempotency_key: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    expected_total_paise: int = Field(gt=0)
    accepted_policy_version: Literal["2026-10-23"]


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    razorpay_order_id: str = Field(max_length=255)
    razorpay_payment_id: str = Field(pattern=r"^pay_[a-zA-Z0-9]+$", max_length=255)
    razorpay_signature: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.get(prefix + "/access")
async def access(org_id: uuid.UUID, tenant=Depends(get_current_tenant_member), db: AsyncSession = Depends(get_db)):
    sub = await db.scalar(select(OrganizationSubscription).where(OrganizationSubscription.organization_id == org_id))
    plan = await db.get(Plan, sub.plan_id) if sub else None
    return {**saas.access_state(sub), "enforcement_enabled": settings.SAAS_ACCESS_ENFORCEMENT_ENABLED,
            "plan_name": plan.name if plan else None, "plan_slug": plan.slug if plan else None}


@router.get(prefix)
async def summary(org_id: uuid.UUID, tenant=Depends(owner), db: AsyncSession = Depends(get_db)):
    sub = await db.scalar(select(OrganizationSubscription).where(OrganizationSubscription.organization_id == org_id))
    plan = await db.get(Plan, sub.plan_id) if sub else None
    purchases = (await db.scalars(select(SaaSCheckout).where(SaaSCheckout.organization_id == org_id)
                                 .order_by(SaaSCheckout.created_at.desc()).limit(50))).all()
    plans = (await db.scalars(select(Plan).where(Plan.slug.in_(["core", "studio"]), Plan.status == "ACTIVE")
                             .order_by(Plan.price))).all()
    ready = saas.charges_ready() and saas.credentials_ready()
    return {"studio_name": tenant[0].name, "access": saas.access_state(sub),
            "enforcement_enabled": settings.SAAS_ACCESS_ENFORCEMENT_ENABLED,
            "plan_name": plan.name if plan else None, "checkout_available": ready,
            "checkout_message": "" if ready else "Online payment setup is in progress. Contact support for billing assistance.",
            "policy_version": saas.POLICY_VERSION,
            "plans": [{"id": str(p.id), "slug": p.slug, "name": p.name,
                       "base_paise": int(p.price * 100), "currency": p.currency,
                       "interval": p.billing_interval} for p in plans],
            "purchases": [saas.checkout_json(p) for p in purchases]}


@router.post(prefix + "/quote")
async def quote(org_id: uuid.UUID, payload: QuoteRequest, tenant=Depends(owner), db: AsyncSession = Depends(get_db)):
    await saas.domestic_only(db, org_id, payload.billing_country)
    plan = await db.get(Plan, payload.plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    return saas.quote_plan(plan)


@router.post(prefix + "/checkouts")
async def checkout(org_id: uuid.UUID, payload: CheckoutRequest, tenant=Depends(owner),
                   user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    row = await saas.create_checkout(db, org_id, user.id, payload.plan_id, payload.billing_country,
                                     payload.idempotency_key, payload.expected_total_paise)
    return saas.checkout_json(row)


async def find_checkout(db, org_id, checkout_id):
    row = await db.scalar(select(SaaSCheckout).where(SaaSCheckout.id == checkout_id, SaaSCheckout.organization_id == org_id))
    if not row:
        raise HTTPException(404, "Checkout not found")
    return row


@router.post(prefix + "/checkouts/{checkout_id}/verify")
async def verify(org_id: uuid.UUID, checkout_id: uuid.UUID, payload: VerifyRequest, tenant=Depends(owner),
                 db: AsyncSession = Depends(get_db)):
    row = await find_checkout(db, org_id, checkout_id)
    if row.provider_order_id != payload.razorpay_order_id or row.provider_key_id != settings.RAZORPAY_KEY_ID:
        raise HTTPException(400, "Payment order does not match checkout")
    saas.verify_signature(row, payload.razorpay_payment_id, payload.razorpay_signature)
    payment = await saas.provider_request("GET", f"payments/{payload.razorpay_payment_id}")
    row = await saas.fulfill(db, row, payment)
    await db.commit()
    return saas.checkout_json(row)


@router.post(prefix + "/checkouts/{checkout_id}/reconcile")
async def reconcile(org_id: uuid.UUID, checkout_id: uuid.UUID, tenant=Depends(owner), db: AsyncSession = Depends(get_db)):
    row = await find_checkout(db, org_id, checkout_id)
    if row.status == "PAID":
        return saas.checkout_json(row)
    if not row.provider_order_id or row.provider_key_id != settings.RAZORPAY_KEY_ID:
        raise HTTPException(409, "Checkout requires support review before another payment is attempted")
    result = await saas.provider_request("GET", f"orders/{row.provider_order_id}/payments")
    for payment in result.get("items", []):
        if payment.get("status") == "captured":
            row = await saas.fulfill(db, row, payment)
            await db.commit()
            break
    return saas.checkout_json(row)


@router.post("/saas-billing/webhooks/razorpay")
async def webhook(request: Request, db: AsyncSession = Depends(get_db)):
    secret = settings.SAAS_RAZORPAY_WEBHOOK_SECRET
    if not secret or "mock" in secret.lower():
        raise HTTPException(503, "SaaS payment webhooks are not configured")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 262144:
            raise HTTPException(413, "Webhook payload too large")
    expected = hmac.new(secret.encode(), bytes(raw), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("X-Razorpay-Signature", "")):
        raise HTTPException(400, "Invalid webhook signature")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid webhook payload")
    event_id = request.headers.get("x-razorpay-event-id") or hashlib.sha256(raw).hexdigest()
    if len(event_id) > 255:
        raise HTTPException(400, "Invalid event identifier")
    row = await db.scalar(select(SaaSWebhookEvent).where(SaaSWebhookEvent.event_id == event_id))
    if not row:
        row = SaaSWebhookEvent(event_id=event_id, event_type=str(payload.get("event", "unknown"))[:100], payload=payload)
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
            await db.commit()
        except IntegrityError:
            row = await db.scalar(select(SaaSWebhookEvent).where(SaaSWebhookEvent.event_id == event_id))
    if row.status in {"PROCESSED", "IGNORED"}:
        return {"status": row.status}
    if row.event_type != "payment.captured":
        row.status = "IGNORED"
    else:
        try:
            payment_data = row.payload["payload"]["payment"]["entity"]
            order_id, payment_id = payment_data["order_id"], payment_data["id"]
            if not isinstance(payment_id, str) or not payment_id.startswith("pay_") or not payment_id.replace("_", "").isalnum():
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "Invalid captured payment event")
        purchase = await db.scalar(select(SaaSCheckout).where(SaaSCheckout.provider_order_id == order_id))
        if purchase:
            payment = await saas.provider_request("GET", f"payments/{payment_id}")
            await saas.fulfill(db, purchase, payment)
            row.status = "PROCESSED"
        else:
            from app.models.commerce import Order
            student_order = await db.scalar(select(Order.id).where(Order.provider_order_id == order_id))
            if student_order:
                row.status = "IGNORED"
            else:
                row.status = "UNMATCHED"
                await db.commit()
                raise HTTPException(503, "Payment association pending reconciliation")
    row.processed_at = saas.now_utc()
    await db.commit()
    return {"status": row.status}
