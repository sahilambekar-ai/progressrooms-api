import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token
from app.main import app, api_v1
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.models.plan import Plan, OrganizationSubscription
from app.models.saas_billing import SaaSCheckout
from app.models.user import User
from app.modules.billing import saas


@pytest.fixture(autouse=True)
def billing_config(monkeypatch):
    for key, value in {"SAAS_GST_BPS": 1800, "SAAS_PLATFORM_FEE_BPS": 0,
        "SAAS_PLATFORM_FEE_PAISE": 0, "SAAS_GST_ON_PLATFORM_FEE": False,
        "RAZORPAY_KEY_ID": "rzp_test_configured", "RAZORPAY_KEY_SECRET": "test-key-secret",
        "SAAS_RAZORPAY_WEBHOOK_SECRET": "test-webhook-secret"}.items():
        monkeypatch.setattr(settings, key, value)


@pytest.mark.parametrize("price,total,gst", [(1499, 176882, 26982), (3999, 471882, 71982)])
def test_exact_customer_totals(price, total, gst):
    plan = SimpleNamespace(id=uuid.uuid4(), name="Plan", slug="studio", status="ACTIVE",
                           price=price, currency="INR", billing_interval="MONTHLY")
    quote = saas.quote_plan(plan)
    assert quote["total_paise"] == total
    assert quote["gst_paise"] == gst
    assert quote["platform_fee_paise"] == 0  # Merchant processing costs do not inflate checkout.
    assert quote["usage_included"] is False


def test_missing_tax_configuration_does_not_silently_charge_zero(monkeypatch):
    monkeypatch.setattr(settings, "SAAS_GST_BPS", None)
    assert not saas.charges_ready()
    with pytest.raises(HTTPException) as error:
        saas.quote_plan(None)
    assert error.value.status_code == 503


@pytest.mark.parametrize("elapsed,state", [(28 - 1/86400, "TRIAL"), (28, "TRIAL_ENDING"),
    (30 - 1/86400, "TRIAL_ENDING"), (30, "GRACE"), (33 - 1/86400, "GRACE"), (33, "RESTRICTED"), (34, "RESTRICTED")])
def test_trial_boundaries(elapsed, state):
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    sub = SimpleNamespace(saas_managed=True, status="TRIAL", starts_at=start, ends_at=None,
                          trial_ends_at=start + timedelta(days=30), trial_grace_ends_at=start + timedelta(days=33))
    assert saas.access_state(sub, start + timedelta(days=elapsed))["state"] == state


def test_legacy_and_invalid_paid_states():
    assert saas.access_state(None)["restricted"] is False
    sub = SimpleNamespace(saas_managed=True, status="ACTIVE", starts_at=saas.now_utc(), ends_at=None,
                          trial_ends_at=None, trial_grace_ends_at=None)
    assert saas.access_state(sub)["restricted"] is True


def test_calendar_month_anchor():
    start = datetime(2028, 1, 31, tzinfo=timezone.utc)
    february = saas.next_month(start, 31)
    assert february.day == 29
    assert saas.next_month(february, 31).day == 31


async def setup(db, country="India"):
    org = Organization(name="SaaS studio", slug=uuid.uuid4().hex)
    user = User(full_name="Owner", email=f"{uuid.uuid4().hex}@test.com", role="STUDIO",
                is_verified=True, account_status="ACTIVE")
    db.add_all([org, user]); await db.flush()
    db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role="STUDIO"))
    db.add(OrganizationSetting(organization_id=org.id, country=country))
    plan = await db.scalar(select(Plan).where(Plan.slug == "studio"))
    sub = await saas.initialize_subscription(db, org.id, "studio", verified=True)
    await db.commit()
    return org, user, plan, sub


@pytest.mark.asyncio
async def test_trial_initializes_once_after_verification(db_session):
    org, user, plan, sub = await setup(db_session)
    sub.status = "PENDING_VERIFICATION"
    sub.trial_starts_at = sub.trial_ends_at = sub.trial_grace_ends_at = None
    await db_session.commit()
    await saas.start_verified_trial(db_session, org.id)
    original = sub.trial_starts_at
    await saas.start_verified_trial(db_session, org.id)
    assert saas.ensure_utc(sub.trial_starts_at) == original
    assert saas.ensure_utc(sub.trial_ends_at) - original == timedelta(days=30)
    assert saas.ensure_utc(sub.trial_grace_ends_at) - original == timedelta(days=33)
    assert (await saas.initialize_subscription(db_session, org.id, "core")).id == sub.id


@pytest.mark.asyncio
async def test_international_country_cannot_use_domestic_checkout(db_session):
    org, _, _, _ = await setup(db_session, "United Kingdom")
    with pytest.raises(HTTPException, match="International"):
        await saas.domestic_only(db_session, org.id, "IN")


@pytest.mark.asyncio
async def test_checkout_retry_price_tampering_and_exactly_once_payment(db_session, monkeypatch):
    org, user, plan, sub = await setup(db_session)
    provider = AsyncMock(return_value={"id": f"order_{uuid.uuid4().hex}", "amount": 471882, "currency": "INR"})
    monkeypatch.setattr(saas, "provider_request", provider)
    with pytest.raises(HTTPException) as error:
        await saas.create_checkout(db_session, org.id, user.id, plan.id, "IN", "bad-price", 399900)
    assert error.value.status_code == 409
    row = await saas.create_checkout(db_session, org.id, user.id, plan.id, "IN", "same-reference", 471882)
    retry = await saas.create_checkout(db_session, org.id, user.id, plan.id, "IN", "same-reference", 471882)
    other_click = await saas.create_checkout(db_session, org.id, user.id, plan.id, "IN", "another-reference", 471882)
    assert retry.id == other_click.id == row.id and provider.await_count == 1
    assert provider.call_args.kwargs["json"]["amount"] == 471882
    payment = {"id": f"pay_{uuid.uuid4().hex}", "order_id": row.provider_order_id, "amount": 471882,
               "currency": "INR", "status": "captured"}
    for mutation in [{"amount": 399900}, {"status": "authorized"}, {"currency": "USD"}, {"order_id": "order_wrong"}, {"amount_refunded": 1}]:
        with pytest.raises(HTTPException):
            await saas.fulfill(db_session, row, {**payment, **mutation})
    await saas.fulfill(db_session, row, payment)
    await db_session.commit()
    assert row.service_starts_at == sub.trial_ends_at
    end = sub.ends_at
    await saas.fulfill(db_session, row, payment)
    assert sub.ends_at == end and row.status == "PAID"
    assert saas.access_state(sub)["state"] == "ACTIVE"
    assert await db_session.scalar(select(func.count(SaaSCheckout.id)).where(SaaSCheckout.organization_id == org.id)) == 1


@pytest.mark.asyncio
async def test_provider_timeout_preserves_intent_and_does_not_create_second_order(db_session, monkeypatch):
    org, user, plan, _ = await setup(db_session)
    provider = AsyncMock(side_effect=HTTPException(503, "Timeout"))
    monkeypatch.setattr(saas, "provider_request", provider)
    with pytest.raises(HTTPException):
        await saas.create_checkout(db_session, org.id, user.id, plan.id, "IN", "timeout-reference", 471882)
    retry = await saas.create_checkout(db_session, org.id, user.id, plan.id, "IN", "another-click", 471882)
    assert retry.status == "UNKNOWN" and provider.await_count == 1


@pytest.mark.asyncio
async def test_tenant_authorization_restricted_recovery_and_webhook(db_session, monkeypatch):
    org, user, plan, sub = await setup(db_session)
    other, stranger, _, _ = await setup(db_session)
    sub.trial_ends_at = saas.now_utc() - timedelta(days=4)
    sub.trial_grace_ends_at = saas.now_utc() - timedelta(days=1)
    await db_session.commit()
    monkeypatch.setattr(settings, "SAAS_ACCESS_ENFORCEMENT_ENABLED", True)
    order_id, payment_id = f"order_{uuid.uuid4().hex}", f"pay_{uuid.uuid4().hex}"
    provider = AsyncMock(return_value={"id": order_id, "amount": 471882, "currency": "INR"})
    monkeypatch.setattr(saas, "provider_request", provider)
    row = await saas.create_checkout(db_session, org.id, user.id, plan.id, "IN", "api-reference", 471882)
    async def override():
        yield db_session
    app.dependency_overrides[get_db] = override
    api_v1.dependency_overrides[get_db] = override
    headers = {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}
    foreign = {"Authorization": f"Bearer {create_access_token({'sub': str(stranger.id)})}"}
    path = f"/api/v1/organizations/{org.id}"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(path + "/saas-billing", headers=headers)).status_code == 200
            assert (await client.get(path + "/profile", headers=headers)).status_code == 200
            assert (await client.get(path + "/members", headers=headers)).status_code == 402
            assert (await client.get(path + "/saas-billing", headers=foreign)).status_code == 403
            forged = await client.post(path + f"/saas-billing/checkouts/{row.id}/verify", headers=headers,
                json={"razorpay_order_id": order_id, "razorpay_payment_id": payment_id, "razorpay_signature": "0" * 64})
            assert forged.status_code == 400
            payment = {"id": payment_id, "order_id": order_id, "amount": 471882, "currency": "INR", "status": "captured"}
            provider.return_value = payment
            raw = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": payment}}}).encode()
            sig = hmac.new(settings.SAAS_RAZORPAY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
            hook_headers = {"X-Razorpay-Signature": sig, "x-razorpay-event-id": uuid.uuid4().hex}
            hook = "/api/v1/saas-billing/webhooks/razorpay"
            assert (await client.post(hook, content=raw, headers={**hook_headers, "X-Razorpay-Signature": "wrong"})).status_code == 400
            assert (await client.post(hook, content=raw, headers=hook_headers)).status_code == 200
            end = sub.ends_at
            assert (await client.post(hook, content=raw, headers=hook_headers)).status_code == 200
            assert sub.ends_at == end
            assert (await client.get(path + "/members", headers=headers)).status_code == 200
    finally:
        app.dependency_overrides.clear(); api_v1.dependency_overrides.clear()

@pytest.mark.parametrize("start,end", [
    (datetime(2026, 1, 31, tzinfo=timezone.utc), datetime(2026, 2, 28, tzinfo=timezone.utc)),
    (datetime(2026, 3, 31, tzinfo=timezone.utc), datetime(2026, 4, 30, tzinfo=timezone.utc)),
    (datetime(2026, 7, 31, tzinfo=timezone.utc), datetime(2026, 8, 31, tzinfo=timezone.utc)),
])
def test_paid_month_end_warning_grace_and_restriction(start, end):
    assert saas.next_month(start, 31) == end
    sub = SimpleNamespace(saas_managed=True, status="ACTIVE", starts_at=start, ends_at=end,
                          trial_ends_at=None, trial_grace_ends_at=None)
    for instant, expected in [
        (end - timedelta(days=2, seconds=1), "ACTIVE"),
        (end - timedelta(days=2), "PAID_ENDING"),
        (end - timedelta(seconds=1), "PAID_ENDING"),
        (end, "PAID_GRACE"),
        (end + timedelta(days=3) - timedelta(seconds=1), "PAID_GRACE"),
        (end + timedelta(days=3), "RESTRICTED"),
    ]:
        result = saas.access_state(sub, instant)
        assert result["state"] == expected
        assert result["restricted"] == (expected == "RESTRICTED")
        assert result["restriction_at"] == end + timedelta(days=3)
        assert result["warning"] == (expected in {"PAID_ENDING", "PAID_GRACE"})

@pytest.mark.asyncio
@pytest.mark.parametrize("days_after_expiry,expected,operational_status", [
    (-2, "PAID_ENDING", 200), (0, "PAID_GRACE", 200),
    (3 - 1/86400, "PAID_GRACE", 200), (3, "RESTRICTED", 402),
])
async def test_paid_access_endpoint_enforces_deadline_and_exposes_plan(db_session, monkeypatch, days_after_expiry, expected, operational_status):
    org, user, plan, sub = await setup(db_session)
    expiry = datetime(2026, 4, 30, tzinfo=timezone.utc)
    sub.status, sub.starts_at, sub.ends_at = "ACTIVE", expiry - timedelta(days=30), expiry
    await db_session.commit()
    monkeypatch.setattr(saas, "now_utc", lambda: expiry + timedelta(days=days_after_expiry))
    monkeypatch.setattr(settings, "SAAS_ACCESS_ENFORCEMENT_ENABLED", True)
    async def override():
        yield db_session
    api_v1.dependency_overrides[get_db] = override
    path = f"/api/v1/organizations/{org.id}"
    headers = {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            access = (await client.get(path + "/saas-billing/access", headers=headers)).json()
            assert access["state"] == expected
            assert access["plan_name"] == "Studio" and access["plan_slug"] == "studio"
            assert (await client.get(path + "/members", headers=headers)).status_code == operational_status
            assert (await client.get(path + "/profile", headers=headers)).status_code == 200
            assert (await client.get(path + "/saas-billing", headers=headers)).status_code == 200
    finally:
        api_v1.dependency_overrides.clear()
