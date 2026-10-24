import pytest
import pytest_asyncio
import uuid
import httpx
from datetime import datetime, timedelta, timezone
from sqlalchemy import select

from app.main import app, api_v1
from app.core.database import get_db
from app.models.user import User
from app.models.organization import Organization, OrganizationMember
from app.models.commerce import Order, Payment
from app.models.communication import (
    PlatformCommunicationSetting,
    CommunicationLog,
    UserCommunicationPreference,
    StudioCommunicationBudget,
)
from app.core.security import create_access_token
from app.modules.communication.service import (
    get_or_create_settings,
    get_or_create_studio_budget,
    check_and_increment_studio_budget,
)
from app.modules.communication.billing_engine import (
    notify_student_payment_received,
    notify_studio_invoice_issued,
    notify_studio_payment_due_soon,
    notify_studio_payment_overdue,
    notify_studio_payment_received,
)


@pytest_asyncio.fixture
async def client(db_session):
    async def dependency():
        yield db_session
    api_v1.dependency_overrides[get_db] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1") as client:
        yield client
    api_v1.dependency_overrides.clear()


@pytest.fixture
def auth_headers():
    def _make(user: User):
        token = create_access_token({"sub": str(user.id), "role": user.role, "email": user.email})
        return {"Authorization": f"Bearer {token}"}
    return _make


async def get_or_create_superadmin(db_session):
    stmt = select(User).where(User.role == "SUPER_ADMIN")
    admin = (await db_session.execute(stmt)).scalar_one_or_none()
    if not admin:
        admin = User(
            email=f"admin_{uuid.uuid4().hex[:6]}@progressrooms.local",
            full_name="Platform Admin",
            role="SUPER_ADMIN",
            is_superadmin=True,
            is_active=True,
            is_verified=True,
        )
        db_session.add(admin)
        await db_session.commit()
    return admin


async def setup_billing_test_data(db_session):
    # Ensure platform switches are ON
    settings = await get_or_create_settings(db_session)
    settings.whatsapp_enabled = True
    settings.email_enabled = True
    await db_session.commit()

    org = Organization(name="Sattva Sanctuary", slug=f"sattva-{uuid.uuid4().hex[:6]}", timezone="Asia/Kolkata")
    db_session.add(org)
    await db_session.flush()

    # Studio Owner (Billing contact)
    owner_user = User(
        email=f"owner_{uuid.uuid4().hex[:6]}@sattva.test",
        full_name="Ananya Desai",
        phone="+91 98765 88990",
        whatsapp_number="+91 98765 88990",
        role="STUDIO",
        is_active=True,
        is_verified=True,
    )
    db_session.add(owner_user)
    await db_session.flush()

    member = OrganizationMember(
        organization_id=org.id,
        user_id=owner_user.id,
        role="STUDIO",
        is_active=True
    )
    db_session.add(member)
    await db_session.flush()

    # Student
    student_user = User(
        email=f"student_{uuid.uuid4().hex[:6]}@example.com",
        full_name="Kabir Mehta",
        phone="+91 98765 22334",
        whatsapp_number="+91 98765 22334",
        role="STUDENT",
        location="Bengaluru",
        is_active=True,
        is_verified=True,
    )
    db_session.add(student_user)
    await db_session.flush()

    # Order
    order = Order(
        organization_id=org.id,
        user_id=student_user.id,
        order_number=f"PR-{uuid.uuid4().hex[:12].upper()}",
        total_amount=1500.0,
        currency="INR",
        status="PAID",
        offering_label="10-Class Prana Pass",
        offering_type="CLASS_PASS",
        credit_limit=10,
        validity_days=30
    )
    db_session.add(order)
    await db_session.flush()

    # Payment
    payment = Payment(
        order_id=order.id,
        amount=order.total_amount,
        currency=order.currency,
        payment_provider="RAZORPAY",
        provider_order_id=f"order_{uuid.uuid4().hex[:8]}",
        provider_payment_id=f"pay_{uuid.uuid4().hex[:8]}",
        provider_signature="sig_test_123",
        status="SUCCESS",
        idempotency_key=f"pay_{uuid.uuid4().hex[:8]}"
    )
    db_session.add(payment)
    await db_session.commit()

    return org, owner_user, student_user, order, payment


@pytest.mark.asyncio
async def test_student_payment_received_notification(db_session):
    """
    Verifies that successful payment triggers notifications via Progress Rooms Admin WhatsApp
    and Brevo email, properly identifying studio and student.
    """
    org, owner, student, order, payment = await setup_billing_test_data(db_session)

    res = await notify_student_payment_received(db_session, order.id)
    assert res["success"] is True
    assert res["dispatched"]["whatsapp"] is True
    assert res["dispatched"]["email"] is True

    # Inspect logs
    logs = (await db_session.scalars(
        select(CommunicationLog).where(
            CommunicationLog.recipient_user_id == student.id,
            CommunicationLog.message_type == "PAYMENT_RECEIVED"
        )
    )).all()

    assert len(logs) == 2
    wa_log = next(l for l in logs if l.channel == "WHATSAPP_ADMIN")
    em_log = next(l for l in logs if l.channel == "EMAIL")

    assert wa_log.status == "SENT"
    assert "Sattva Sanctuary" in wa_log.body_preview
    assert "10-Class Prana Pass" in wa_log.body_preview

    assert em_log.status == "SENT"
    assert "10-Class Prana Pass" in em_log.subject_or_title


@pytest.mark.asyncio
async def test_studio_subscription_billing_notifications(db_session):
    """
    Verifies studio subscription billing dispatches:
    - Invoice issued
    - Payment due soon (T-3 days)
    - Payment overdue (T+3 days)
    - Payment received
    """
    org, owner, student, order, payment = await setup_billing_test_data(db_session)

    due_date = datetime.now(timezone.utc) + timedelta(days=3)

    # 1. Invoice issued
    inv_res = await notify_studio_invoice_issued(
        db=db_session,
        organization_id=org.id,
        invoice_number="INV-2026-001",
        amount_inr=4999.0,
        billing_period="October 2026",
        due_date=due_date
    )
    assert inv_res["success"] is True
    assert inv_res["dispatches_sent"] == 2  # WhatsApp + Email

    # 2. Payment due soon
    due_res = await notify_studio_payment_due_soon(
        db=db_session,
        organization_id=org.id,
        invoice_number="INV-2026-001",
        amount_inr=4999.0,
        due_date=due_date
    )
    assert due_res["success"] is True
    assert due_res["dispatches_sent"] == 2

    # 3. Payment overdue
    overdue_res = await notify_studio_payment_overdue(
        db=db_session,
        organization_id=org.id,
        invoice_number="INV-2026-001",
        amount_inr=4999.0,
        days_overdue=3
    )
    assert overdue_res["success"] is True
    assert overdue_res["dispatches_sent"] == 2

    # 4. Payment received
    recv_res = await notify_studio_payment_received(
        db=db_session,
        organization_id=org.id,
        amount_inr=4999.0,
        payment_reference="pay_sub_oct26"
    )
    assert recv_res["success"] is True
    assert recv_res["dispatches_sent"] == 2


@pytest.mark.asyncio
async def test_studio_budget_limits_and_reserve_critical_policy(db_session):
    """
    Verifies that:
    1. A studio with low monthly allowance (e.g. ₹1.00) allows first message (₹0.75).
    2. A second routine reminder (₹0.75, totaling ₹1.50) is SUPPRESSED with:
       "Suppressed: studio budget exceeded (reserved for critical)".
    3. Urgent notifications (CLASS_CANCELLATION, CORRECTION_CONSOLIDATED) are ALLOWED through
       even when over budget under RESERVE_CRITICAL policy!
    """
    org, owner, student, order, payment = await setup_billing_test_data(db_session)

    # Set monthly allowance to ₹1.00 with RESERVE_CRITICAL policy
    budget = await get_or_create_studio_budget(db_session, org.id)
    budget.monthly_allowance_inr = 1.00
    budget.current_month_usage_inr = 0.0
    budget.overage_policy = "RESERVE_CRITICAL"
    await db_session.commit()

    # 1. First routine message (REMINDER_2H): Cost ₹0.75 <= ₹1.00 -> ALLOWED
    ok1, reason1 = await check_and_increment_studio_budget(
        db=db_session,
        organization_id=org.id,
        channel="WHATSAPP_CLASSES",
        message_type="REMINDER_2H"
    )
    assert ok1 is True
    assert reason1 is None
    assert budget.current_month_usage_inr == 0.75
    assert budget.current_month_message_count == 1

    # 2. Second routine message (REMINDER_15M): Cost ₹0.75 + ₹0.75 = ₹1.50 > ₹1.00 -> SUPPRESSED
    ok2, reason2 = await check_and_increment_studio_budget(
        db=db_session,
        organization_id=org.id,
        channel="WHATSAPP_CLASSES",
        message_type="REMINDER_15M"
    )
    assert ok2 is False
    assert reason2 == "Suppressed: studio budget exceeded (reserved for critical)"
    assert budget.current_month_usage_inr == 0.75  # Unchanged

    # 3. Urgent cancellation message (CLASS_CANCELLATION): Even over budget, allowed under RESERVE_CRITICAL!
    ok3, reason3 = await check_and_increment_studio_budget(
        db=db_session,
        organization_id=org.id,
        channel="WHATSAPP_CLASSES",
        message_type="CLASS_CANCELLATION"
    )
    assert ok3 is True
    assert reason3 is None
    assert budget.current_month_usage_inr == 1.50
    assert budget.current_month_message_count == 2


@pytest.mark.asyncio
async def test_studio_budget_alert_thresholds(db_session):
    """
    Verifies that crossing 70%, 90%, and 100% of budget allowance
    updates the alert_level_sent tracker in StudioCommunicationBudget.
    """
    org, owner, student, order, payment = await setup_billing_test_data(db_session)

    budget = await get_or_create_studio_budget(db_session, org.id)
    budget.monthly_allowance_inr = 1.00  # ₹1.00
    budget.current_month_usage_inr = 0.0
    budget.alert_level_sent = 0
    budget.overage_policy = "ALLOW_OVERAGE"
    await db_session.commit()

    # Dispatch 1 WhatsApp message (₹0.75 = 75% of ₹1.00)
    await check_and_increment_studio_budget(
        db=db_session,
        organization_id=org.id,
        channel="WHATSAPP_CLASSES",
        message_type="REMINDER_2H"
    )
    assert budget.alert_level_sent == 70

    # Dispatch 1 email (₹0.05, total ₹0.80 = 80%)
    await check_and_increment_studio_budget(
        db=db_session,
        organization_id=org.id,
        channel="EMAIL",
        message_type="REMINDER_2H"
    )
    assert budget.alert_level_sent == 70  # Still 70 until 90%

    # Dispatch 2 more emails (total ₹0.90 = 90%)
    await check_and_increment_studio_budget(
        db=db_session,
        organization_id=org.id,
        channel="EMAIL",
        message_type="REMINDER_2H"
    )
    await check_and_increment_studio_budget(
        db=db_session,
        organization_id=org.id,
        channel="EMAIL",
        message_type="REMINDER_2H"
    )
    assert budget.alert_level_sent == 90

    # Dispatch another WhatsApp message (total ₹1.65 >= 100%)
    await check_and_increment_studio_budget(
        db=db_session,
        organization_id=org.id,
        channel="WHATSAPP_CLASSES",
        message_type="REMINDER_2H"
    )
    assert budget.alert_level_sent == 100


@pytest.mark.asyncio
async def test_engine_cycle_and_budgets_api(db_session, client, auth_headers):
    """
    Verifies:
    1. GET /admin/communication/budgets lists studio allowances and usage.
    2. PATCH /admin/communication/budgets/{org_id} updates allowance and policy.
    3. POST /admin/communication/run-engine-cycle executes notification cycle.
    """
    superadmin = await get_or_create_superadmin(db_session)
    org, owner, student, order, payment = await setup_billing_test_data(db_session)

    # 1. GET /budgets
    res_list = await client.get("/admin/communication/budgets", headers=auth_headers(superadmin))
    assert res_list.status_code == 200
    budgets = res_list.json()
    assert len(budgets) >= 1
    target = next((b for b in budgets if b["organization_id"] == str(org.id)), None)
    assert target is not None
    assert target["monthly_allowance_inr"] == 1000.0

    # 2. PATCH /budgets/{org_id}
    res_patch = await client.patch(
        f"/admin/communication/budgets/{org.id}",
        json={"monthly_allowance_inr": 2500.0, "overage_policy": "ALLOW_OVERAGE"},
        headers=auth_headers(superadmin),
    )
    assert res_patch.status_code == 200
    patched = res_patch.json()
    assert patched["monthly_allowance_inr"] == 2500.0
    assert patched["overage_policy"] == "ALLOW_OVERAGE"

    # 3. POST /run-engine-cycle
    res_cycle = await client.post("/admin/communication/run-engine-cycle", headers=auth_headers(superadmin))
    assert res_cycle.status_code == 200
    cycle_data = res_cycle.json()
    assert "timestamp" in cycle_data
    assert "reminders_dispatched" in cycle_data
    assert "corrections_dispatched" in cycle_data
    assert "billing_summary" in cycle_data
    assert "Engine cycle completed" in cycle_data["details"]
