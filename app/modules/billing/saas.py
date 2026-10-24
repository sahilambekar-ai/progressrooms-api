"""Exact-price platform checkout and a deterministic trial lifecycle."""
import calendar
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import httpx
from fastapi import HTTPException
from sqlalchemy import select
from app.common.dates import ensure_utc
from app.core.config import settings
from app.models.organization import Organization, OrganizationSetting
from app.models.plan import Plan, OrganizationSubscription
from app.models.saas_billing import SaaSCheckout

POLICY_VERSION = "2026-10-23"
SUPPORT_MESSAGE = "International pricing varies by country. Contact support for a quote."


def now_utc():
    return datetime.now(timezone.utc)


def credentials_ready():
    return all(value and "mock" not in value.lower() for value in (
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET, settings.SAAS_RAZORPAY_WEBHOOK_SECRET))


def charges_ready():
    values = (settings.SAAS_GST_BPS, settings.SAAS_PLATFORM_FEE_BPS, settings.SAAS_PLATFORM_FEE_PAISE)
    return (all(isinstance(value, int) and value >= 0 for value in values)
            and settings.SAAS_GST_BPS <= 10000 and settings.SAAS_PLATFORM_FEE_BPS <= 10000
            and settings.SAAS_GST_ON_PLATFORM_FEE is not None)


def quote_plan(plan):
    if not charges_ready():
        raise HTTPException(503, "Tax and platform fee configuration is pending. Contact support before payment.")
    if plan.slug not in {"core", "studio"} or plan.status != "ACTIVE" or plan.currency != "INR" or plan.billing_interval != "MONTHLY":
        raise HTTPException(422, "This plan is not available for online purchase")
    base = int((Decimal(str(plan.price)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if base <= 0:
        raise HTTPException(422, "This plan needs a valid price")
    def percentage(amount, bps):
        return int((Decimal(amount) * Decimal(bps) / 10000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    fee = settings.SAAS_PLATFORM_FEE_PAISE + percentage(base, settings.SAAS_PLATFORM_FEE_BPS)
    tax = percentage(base + (fee if settings.SAAS_GST_ON_PLATFORM_FEE else 0), settings.SAAS_GST_BPS)
    return {"plan_id": str(plan.id), "plan_name": plan.name, "plan_slug": plan.slug,
            "interval": "MONTHLY", "currency": "INR", "base_paise": base,
            "platform_fee_paise": fee, "gst_paise": tax, "total_paise": base + fee + tax,
            "gst_bps": settings.SAAS_GST_BPS, "fee_bps": settings.SAAS_PLATFORM_FEE_BPS,
            "fixed_fee_paise": settings.SAAS_PLATFORM_FEE_PAISE,
            "gst_on_platform_fee": settings.SAAS_GST_ON_PLATFORM_FEE,
            "policy_version": POLICY_VERSION, "usage_included": False}


def access_state(subscription, now=None):
    now = ensure_utc(now or now_utc())
    if not subscription or not subscription.saas_managed:
        return {"state": "LEGACY", "restricted": False, "warning": False}
    trial_end = ensure_utc(subscription.trial_ends_at)
    grace_end = ensure_utc(subscription.trial_grace_ends_at)
    paid_end = ensure_utc(subscription.ends_at)
    paid_start = ensure_utc(subscription.starts_at)
    if subscription.status == "ACTIVE" and paid_start and paid_start <= now and paid_end and now < paid_end:
        state = "PAID_ENDING" if now >= paid_end - timedelta(days=2) else "ACTIVE"
    elif subscription.status == "ACTIVE" and paid_start and paid_start <= now and paid_end and now < paid_end + timedelta(days=3):
        state = "PAID_GRACE"
    elif subscription.status == "TRIAL" and trial_end and now < trial_end:
        state = "TRIAL_ENDING" if now >= trial_end - timedelta(days=2) else "TRIAL"
    elif subscription.status == "TRIAL" and grace_end and now < grace_end:
        state = "GRACE"
    elif subscription.status == "PENDING_VERIFICATION":
        state = "PENDING_VERIFICATION"
    else:
        state = "RESTRICTED"
    return {"state": state, "restricted": state == "RESTRICTED", "warning": state in {"TRIAL_ENDING", "GRACE", "PAID_ENDING", "PAID_GRACE"},
            "trial_ends_at": trial_end, "restriction_at": paid_end + timedelta(days=3) if subscription.status == "ACTIVE" and paid_end else grace_end,
            "paid_through": paid_end if subscription.status == "ACTIVE" else None}


async def operational_access(db, org_id):
    if not settings.SAAS_ACCESS_ENFORCEMENT_ENABLED:
        return True
    sub = await db.scalar(select(OrganizationSubscription).where(OrganizationSubscription.organization_id == org_id))
    return not access_state(sub)["restricted"]


async def require_operational_access(db, org_id):
    if not await operational_access(db, org_id):
        raise HTTPException(402, "Studio access has expired. The studio owner can renew in Progress Rooms Billing. Billing and Settings remain available.")


async def initialize_subscription(db, org_id, plan_slug="starter", verified=False):
    # Shared organization lock serializes registration, verification and purchases.
    await db.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
    sub = await db.scalar(select(OrganizationSubscription).where(OrganizationSubscription.organization_id == org_id))
    if sub:
        if sub.saas_managed and sub.status == "PENDING_VERIFICATION":
            selected = await db.scalar(select(Plan).where(Plan.slug == plan_slug, Plan.status == "ACTIVE"))
            if not selected or plan_slug not in {"starter", "core", "studio"}:
                raise HTTPException(422, "The selected plan is unavailable")
            sub.plan_id = selected.id
        return sub
    plan = await db.scalar(select(Plan).where(Plan.slug == plan_slug, Plan.status == "ACTIVE"))
    if not plan or plan_slug not in {"starter", "core", "studio"}:
        raise HTTPException(422, "The selected plan is unavailable. Please choose another plan.")
    now = now_utc()
    sub = OrganizationSubscription(organization_id=org_id, plan_id=plan.id, saas_managed=True,
        status="TRIAL" if verified else "PENDING_VERIFICATION", starts_at=now,
        trial_starts_at=now if verified else None,
        trial_ends_at=now + timedelta(days=30) if verified else None,
        trial_grace_ends_at=now + timedelta(days=33) if verified else None)
    db.add(sub)
    await db.flush()
    return sub


async def start_verified_trial(db, org_id):
    await db.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
    sub = await db.scalar(select(OrganizationSubscription).where(OrganizationSubscription.organization_id == org_id)
                          .execution_options(populate_existing=True))
    if sub and sub.saas_managed and sub.status == "PENDING_VERIFICATION" and sub.trial_starts_at is None:
        now = now_utc()
        sub.status = "TRIAL"
        sub.starts_at = sub.trial_starts_at = now
        sub.trial_ends_at = now + timedelta(days=30)
        sub.trial_grace_ends_at = now + timedelta(days=33)


async def domestic_only(db, org_id, country):
    studio_settings = await db.scalar(select(OrganizationSetting).where(OrganizationSetting.organization_id == org_id))
    stored = (studio_settings.country or "").strip().upper() if studio_settings else ""
    if country != "IN" or (stored and stored not in {"IN", "INDIA"}):
        raise HTTPException(422, SUPPORT_MESSAGE)


async def provider_request(method, path, **kwargs):
    if not credentials_ready():
        raise HTTPException(503, "Razorpay checkout is not configured. Contact support.")
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            response = await client.request(method, f"https://api.razorpay.com/v1/{path}",
                auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET), **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            # Do not leak provider credentials, payment data or provider internals.
            raise HTTPException(503, "Payment status could not be confirmed. Please check again before retrying payment.")


def checkout_json(row):
    return {"id": str(row.id), "status": row.status, "quote": row.quote,
            "razorpay_key_id": row.provider_key_id, "razorpay_order_id": row.provider_order_id,
            "paid_at": row.paid_at, "service_starts_at": row.service_starts_at,
            "service_ends_at": row.service_ends_at,
            "receipt_number": f"SAAS-{row.id.hex.upper()}" if row.status == "PAID" else None}


async def create_checkout(db, org_id, user_id, plan_id, country, key, expected_total):
    await domestic_only(db, org_id, country)
    if not credentials_ready():
        raise HTTPException(503, "Razorpay checkout is not configured. Contact support.")
    await db.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
    existing = await db.scalar(select(SaaSCheckout).where(SaaSCheckout.organization_id == org_id,
                                                        SaaSCheckout.idempotency_key == key))
    if existing:
        if existing.plan_id != plan_id:
            raise HTTPException(409, "This checkout reference belongs to another plan")
        return existing
    pending = await db.scalar(select(SaaSCheckout).where(SaaSCheckout.organization_id == org_id,
                                    SaaSCheckout.status.in_(["CREATING", "PENDING", "UNKNOWN"])))
    if pending:
        if pending.plan_id != plan_id:
            raise HTTPException(409, "A payment is already pending for another plan. Contact support before changing it.")
        return pending
    plan = await db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    quote = quote_plan(plan)
    if expected_total != quote["total_paise"]:
        raise HTTPException(409, "The price has changed. Review the updated total before payment.")
    sub = await db.scalar(select(OrganizationSubscription).where(OrganizationSubscription.organization_id == org_id))
    if sub and sub.status == "ACTIVE" and (sub.ends_at is None or ensure_utc(sub.ends_at) > now_utc()) and sub.plan_id != plan.id:
        raise HTTPException(409, "Contact support to change your active plan. You can renew the current plan here.")
    if sub and not sub.saas_managed and sub.status == "ACTIVE" and sub.ends_at is None:
        raise HTTPException(409, "Contact support to review your existing subscription before purchasing.")
    row = SaaSCheckout(organization_id=org_id, purchaser_id=user_id, plan_id=plan.id,
        idempotency_key=key, total_paise=quote["total_paise"], currency="INR", quote=quote,
        provider_key_id=settings.RAZORPAY_KEY_ID)
    db.add(row)
    await db.commit()  # Durable intent survives a provider timeout or process failure.
    try:
        result = await provider_request("POST", "orders", json={"amount": row.total_paise,
            "currency": row.currency, "receipt": row.id.hex, "partial_payment": False,
            "notes": {"purpose": "PROGRESSROOMS_SAAS", "checkout_id": str(row.id)}})
    except HTTPException:
        row.status = "UNKNOWN"
        await db.commit()
        raise
    if not result.get("id") or result.get("amount") != row.total_paise or result.get("currency") != row.currency:
        row.status = "UNKNOWN"
        await db.commit()
        raise HTTPException(503, "Checkout requires payment reconciliation. Contact support.")
    row.provider_order_id, row.status = result["id"], "PENDING"
    await db.commit()
    return row


def next_month(start, anchor_day):
    year, month = start.year + start.month // 12, start.month % 12 + 1
    return start.replace(year=year, month=month, day=min(anchor_day, calendar.monthrange(year, month)[1]))


async def fulfill(db, row, payment):
    await db.execute(select(Organization.id).where(Organization.id == row.organization_id).with_for_update())
    row = await db.scalar(select(SaaSCheckout).where(SaaSCheckout.id == row.id).with_for_update()
                          .execution_options(populate_existing=True))
    if row.provider_key_id != settings.RAZORPAY_KEY_ID:
        raise HTTPException(409, "Payment belongs to a different merchant configuration")
    if (payment.get("order_id") != row.provider_order_id or payment.get("amount") != row.total_paise
            or payment.get("currency") != row.currency or payment.get("status") != "captured"
            or payment.get("amount_refunded", 0) != 0 or not payment.get("id")):
        raise HTTPException(409, "The full captured payment has not been verified")
    if row.status == "PAID":
        if row.provider_payment_id != payment["id"]:
            raise HTTPException(409, "A second payment requires support review")
        return row
    duplicate = await db.scalar(select(SaaSCheckout.id).where(SaaSCheckout.provider_payment_id == payment["id"]))
    if duplicate:
        raise HTTPException(409, "Payment has already been applied")
    sub = await db.scalar(select(OrganizationSubscription).where(OrganizationSubscription.organization_id == row.organization_id))
    now = now_utc()
    if not sub:
        sub = OrganizationSubscription(organization_id=row.organization_id, plan_id=row.plan_id,
                                       starts_at=now, status="ACTIVE", saas_managed=True)
        db.add(sub)
    if sub.status == "ACTIVE" and sub.plan_id != row.plan_id and sub.ends_at and ensure_utc(sub.ends_at) > now:
        raise HTTPException(409, "The studio plan changed while payment was pending. Contact support.")
    start = now
    if sub.status == "ACTIVE" and sub.ends_at and ensure_utc(sub.ends_at) > now:
        start = ensure_utc(sub.ends_at)
    elif sub.status == "TRIAL" and sub.trial_ends_at and ensure_utc(sub.trial_ends_at) > now:
        start = ensure_utc(sub.trial_ends_at)
    last = await db.scalar(select(SaaSCheckout).where(SaaSCheckout.organization_id == row.organization_id,
                          SaaSCheckout.status == "PAID").order_by(SaaSCheckout.service_ends_at.desc()).limit(1))
    anchor = last.quote.get("billing_anchor_day", start.day) if last and last.service_ends_at and ensure_utc(last.service_ends_at) == start else start.day
    end = next_month(start, anchor)
    row.quote = {**row.quote, "billing_anchor_day": anchor}
    row.status, row.provider_payment_id, row.paid_at = "PAID", payment["id"], now
    row.service_starts_at, row.service_ends_at = start, end
    sub.plan_id, sub.status, sub.ends_at, sub.saas_managed = row.plan_id, "ACTIVE", end, True
    sub.starts_at = min(ensure_utc(sub.starts_at), now)
    sub.payment_provider = "RAZORPAY"
    await db.flush()
    return row


def verify_signature(row, payment_id, signature):
    expected = hmac.new(settings.RAZORPAY_KEY_SECRET.encode(),
        f"{row.provider_order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(400, "Invalid payment signature")
