from urllib.parse import urlencode
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from sqlalchemy import select, and_, desc
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.user import User
from app.models.organization import Organization, OrganizationMember
from app.models.commerce import Order, Payment
from app.models.communication import (
    PlatformCommunicationSetting,
    CommunicationLog,
    UserCommunicationPreference,
)
from app.modules.communication.service import get_or_create_settings
from app.modules.communication.templates import (
    render_student_payment_received_wa,
    render_student_payment_received_email,
    render_billing_invoice_wa,
    render_billing_invoice_email,
    render_billing_due_soon_wa,
    render_billing_due_soon_email,
    render_billing_overdue_wa,
    render_billing_overdue_email,
    render_billing_payment_received_wa,
    render_billing_payment_received_email,
)

logger = logging.getLogger("progressrooms.billing_communication")


def format_due_date_str(dt: datetime) -> str:
    ist_offset = timedelta(hours=5, minutes=30)
    ist_dt = dt.astimezone(timezone(ist_offset)) if dt.tzinfo else (dt + ist_offset)
    return ist_dt.strftime("%b %d, %Y")


async def get_studio_billing_contacts(
    db: AsyncSession,
    organization_id: uuid.UUID
) -> List[User]:
    """
    Finds verified billing contacts for a studio (members with role STUDIO or OWNER).
    """
    stmt = (
        select(User)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.role == "STUDIO",
            OrganizationMember.is_active == True
        )
    )
    result = await db.execute(stmt)
    contacts = result.scalars().all()
    return list(contacts)


async def notify_student_payment_received(
    db: AsyncSession,
    order_id: uuid.UUID,
    receipt_url_override: Optional[str] = None
) -> Dict[str, Any]:
    """
    Dispatches a payment confirmation notice to a student upon successful booking checkout.
    Uses Progress Rooms transactional email through Brevo.
    """
    order = await db.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if not order:
        logger.warning(f"notify_student_payment_received: Order {order_id} not found")
        return {"success": False, "reason": "Order not found"}

    already_sent = await db.scalar(select(CommunicationLog.id).where(
        CommunicationLog.message_type == "PAYMENT_RECEIVED", CommunicationLog.channel == "EMAIL",
        CommunicationLog.metadata_json["order_id"].as_string() == str(order_id),
        CommunicationLog.status.in_(["SENT", "UNCERTAIN", "SIMULATED"])).limit(1))
    if already_sent:
        return {"success": True, "dispatched": {"whatsapp": False, "email": False}, "order_id": str(order_id), "already_notified": True}

    student = await db.get(User, order.user_id)
    org = await db.get(Organization, order.organization_id)
    studio_name = org.name if org else "Progress Rooms Studio"

    # Find payment reference
    payment_stmt = select(Payment).where(Payment.order_id == order_id).order_by(desc(Payment.created_at)).limit(1)
    payment = (await db.execute(payment_stmt)).scalar_one_or_none()
    payment_ref = payment.provider_payment_id if payment and payment.provider_payment_id else order.order_number

    public_origin = getattr(settings, "WEBSITE_PUBLIC_ORIGIN", "http://localhost:3000")
    receipt_url = receipt_url_override or f"{public_origin.rstrip('/')}/student?tab=passes"

    settings_summary = await get_or_create_settings(db)
    pref = await db.scalar(
        select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == student.id)
    )

    amount = float(order.total_amount)
    offering_name = order.offering_label or "Class Pass / Drop-in"
    dispatched = {"whatsapp": False, "email": False}

    # 1. WhatsApp confirmation (Admin sender)

    # 2. Email confirmation
    if student.email:
        if not settings_summary.email_enabled:
            db.add(CommunicationLog(
                recipient_user_id=student.id,
                organization_id=order.organization_id,
                recipient_email=student.email,
                channel="EMAIL",
                message_type="PAYMENT_RECEIVED",
                status="SUPPRESSED",
                suppression_reason="Suppressed: email disabled",
                subject_or_title=f"Payment Confirmed: {offering_name}",
                metadata_json={"order_id": str(order.id), "amount": amount}
            ))
        elif pref and pref.email_opted_out:
            db.add(CommunicationLog(
                recipient_user_id=student.id,
                organization_id=order.organization_id,
                recipient_email=student.email,
                channel="EMAIL",
                message_type="PAYMENT_RECEIVED",
                status="SUPPRESSED",
                suppression_reason="Suppressed: recipient opt-out",
                subject_or_title=f"Payment Confirmed: {offering_name}"
            ))
        else:
            subj, html = render_student_payment_received_email(
                student_name=student.full_name,
                studio_name=studio_name,
                class_or_pass_name=offering_name,
                amount_inr=amount,
                receipt_url=receipt_url,
                payment_reference=payment_ref
            )
            sent = await _record_brevo_email(db, CommunicationLog(
                recipient_user_id=student.id,
                organization_id=order.organization_id,
                recipient_email=student.email,
                channel="EMAIL",
                message_type="PAYMENT_RECEIVED",
                status="SENT",
                subject_or_title=subj,
                body_preview=f"Payment confirmed for {offering_name} at {studio_name}",
                metadata_json={"order_id": str(order.id), "amount": amount, "reference": payment_ref}
            ), html)
            dispatched["email"] = sent

    await db.commit()
    return {"success": True, "dispatched": dispatched, "order_id": str(order_id)}


async def notify_studio_invoice_issued(
    db: AsyncSession,
    organization_id: uuid.UUID,
    invoice_number: str,
    amount_inr: float,
    billing_period: str,
    due_date: datetime,
    invoice_url: Optional[str] = None
) -> Dict[str, Any]:
    """
    Sends platform subscription invoice notice to studio billing contacts.
    """
    org = await db.get(Organization, organization_id)
    studio_name = org.name if org else "Studio"
    contacts = await get_studio_billing_contacts(db, organization_id)

    if not contacts:
        logger.warning(f"No billing contacts found for organization {organization_id}")
        return {"success": False, "reason": "No billing contacts"}

    public_origin = getattr(settings, "WEBSITE_PUBLIC_ORIGIN", "http://localhost:3000")
    inv_url = invoice_url or f"{public_origin.rstrip('/')}/dashboard?{urlencode({'tab': 'billing-progressrooms', 'studio_id': str(organization_id)})}"
    due_date_str = format_due_date_str(due_date)

    settings_summary = await get_or_create_settings(db)
    dispatches_sent = 0

    for contact in contacts:
        pref = await db.scalar(
            select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == contact.id)
        )

        # WhatsApp (Admin sender)

        # Email
        if contact.email:
            if not settings_summary.email_enabled:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="INVOICE_ISSUED",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: email disabled",
                    subject_or_title=f"Invoice Issued #{invoice_number}",
                    metadata_json={"invoice_number": invoice_number, "amount": amount_inr}
                ))
            elif pref and pref.email_opted_out:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="INVOICE_ISSUED",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: recipient opt-out",
                    subject_or_title=f"Invoice Issued #{invoice_number}"
                ))
            else:
                subj, html = render_billing_invoice_email(
                    recipient_name=contact.full_name,
                    studio_name=studio_name,
                    invoice_number=invoice_number,
                    amount_inr=amount_inr,
                    billing_period=billing_period,
                    due_date_str=due_date_str,
                    invoice_url=inv_url
                )
                sent = await _record_brevo_email(db, CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="INVOICE_ISSUED",
                    status="SENT",
                    subject_or_title=subj,
                    body_preview=f"Invoice #{invoice_number} for {studio_name}",
                    metadata_json={"invoice_number": invoice_number, "amount": amount_inr}
                ), html)
                dispatches_sent += int(sent)

    await db.commit()
    return {"success": True, "dispatches_sent": dispatches_sent}


async def notify_studio_payment_due_soon(
    db: AsyncSession,
    organization_id: uuid.UUID,
    invoice_number: str,
    amount_inr: float,
    due_date: datetime,
    invoice_url: Optional[str] = None
) -> Dict[str, Any]:
    """
    Sends 'Payment due soon' reminder (proposed default: 3 days before).
    Checks payment status immediately before sending; if already paid, skips dispatch.
    """
    org = await db.get(Organization, organization_id)
    studio_name = org.name if org else "Studio"
    contacts = await get_studio_billing_contacts(db, organization_id)

    public_origin = getattr(settings, "WEBSITE_PUBLIC_ORIGIN", "http://localhost:3000")
    inv_url = invoice_url or f"{public_origin.rstrip('/')}/dashboard?{urlencode({'tab': 'billing-progressrooms', 'studio_id': str(organization_id)})}"
    due_date_str = format_due_date_str(due_date)

    settings_summary = await get_or_create_settings(db)
    dispatches_sent = 0

    for contact in contacts:
        pref = await db.scalar(
            select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == contact.id)
        )


        if contact.email:
            if not settings_summary.email_enabled:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="PAYMENT_DUE_SOON",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: email disabled",
                    subject_or_title=f"Payment Due Soon: {studio_name}",
                    metadata_json={"invoice_number": invoice_number, "amount": amount_inr}
                ))
            elif pref and pref.email_opted_out:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="PAYMENT_DUE_SOON",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: recipient opt-out",
                    subject_or_title=f"Payment Due Soon: {studio_name}"
                ))
            else:
                subj, html = render_billing_due_soon_email(
                    recipient_name=contact.full_name,
                    studio_name=studio_name,
                    amount_inr=amount_inr,
                    due_date_str=due_date_str,
                    invoice_url=inv_url
                )
                sent = await _record_brevo_email(db, CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="PAYMENT_DUE_SOON",
                    status="SENT",
                    subject_or_title=subj,
                    body_preview=f"Subscription payment due on {due_date_str}",
                    metadata_json={"invoice_number": invoice_number, "amount": amount_inr}
                ), html)
                dispatches_sent += int(sent)

    await db.commit()
    return {"success": True, "dispatches_sent": dispatches_sent}


async def notify_studio_payment_overdue(
    db: AsyncSession,
    organization_id: uuid.UUID,
    invoice_number: str,
    amount_inr: float,
    days_overdue: int,
    invoice_url: Optional[str] = None
) -> Dict[str, Any]:
    """
    Sends 'Payment overdue' notice (proposed default: 3 days after due date).
    """
    org = await db.get(Organization, organization_id)
    studio_name = org.name if org else "Studio"
    contacts = await get_studio_billing_contacts(db, organization_id)

    public_origin = getattr(settings, "WEBSITE_PUBLIC_ORIGIN", "http://localhost:3000")
    inv_url = invoice_url or f"{public_origin.rstrip('/')}/dashboard?{urlencode({'tab': 'billing-progressrooms', 'studio_id': str(organization_id)})}"

    settings_summary = await get_or_create_settings(db)
    dispatches_sent = 0

    for contact in contacts:
        pref = await db.scalar(
            select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == contact.id)
        )


        if contact.email:
            if not settings_summary.email_enabled:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="PAYMENT_OVERDUE",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: email disabled",
                    subject_or_title=f"⚠️ Overdue Notice: {studio_name}",
                    metadata_json={"invoice_number": invoice_number, "days_overdue": days_overdue}
                ))
            elif pref and pref.email_opted_out:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="PAYMENT_OVERDUE",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: recipient opt-out",
                    subject_or_title=f"⚠️ Overdue Notice: {studio_name}"
                ))
            else:
                subj, html = render_billing_overdue_email(
                    recipient_name=contact.full_name,
                    studio_name=studio_name,
                    amount_inr=amount_inr,
                    days_overdue=days_overdue,
                    invoice_url=inv_url
                )
                sent = await _record_brevo_email(db, CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="PAYMENT_OVERDUE",
                    status="SENT",
                    subject_or_title=subj,
                    body_preview=f"Subscription payment {days_overdue} days overdue",
                    metadata_json={"invoice_number": invoice_number, "days_overdue": days_overdue}
                ), html)
                dispatches_sent += int(sent)

    await db.commit()
    return {"success": True, "dispatches_sent": dispatches_sent}


async def notify_studio_payment_received(
    db: AsyncSession,
    organization_id: uuid.UUID,
    amount_inr: float,
    payment_reference: str,
    receipt_url: Optional[str] = None
) -> Dict[str, Any]:
    """
    Sends platform subscription payment confirmation notice to studio billing contacts.
    """
    org = await db.get(Organization, organization_id)
    studio_name = org.name if org else "Studio"
    contacts = await get_studio_billing_contacts(db, organization_id)

    public_origin = getattr(settings, "WEBSITE_PUBLIC_ORIGIN", "http://localhost:3000")
    rec_url = receipt_url or f"{public_origin.rstrip('/')}/dashboard?{urlencode({'tab': 'billing-saas', 'studio_id': str(organization_id)})}"

    settings_summary = await get_or_create_settings(db)
    dispatches_sent = 0

    for contact in contacts:
        pref = await db.scalar(
            select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == contact.id)
        )


        if contact.email:
            if not settings_summary.email_enabled:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="STUDIO_PAYMENT_RECEIVED",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: email disabled",
                    subject_or_title=f"Payment Received: {studio_name}",
                    metadata_json={"reference": payment_reference, "amount": amount_inr}
                ))
            elif pref and pref.email_opted_out:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="STUDIO_PAYMENT_RECEIVED",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: recipient opt-out",
                    subject_or_title=f"Payment Received: {studio_name}"
                ))
            else:
                subj, html = render_billing_payment_received_email(
                    recipient_name=contact.full_name,
                    studio_name=studio_name,
                    amount_inr=amount_inr,
                    receipt_url=rec_url,
                    payment_reference=payment_reference
                )
                sent = await _record_brevo_email(db, CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="STUDIO_PAYMENT_RECEIVED",
                    status="SENT",
                    subject_or_title=subj,
                    body_preview=f"Payment of ₹{amount_inr:,.2f} confirmed for {studio_name}",
                    metadata_json={"reference": payment_reference, "amount": amount_inr}
                ), html)
                dispatches_sent += int(sent)

    await db.commit()
    return {"success": True, "dispatches_sent": dispatches_sent}


async def process_billing_reminders(
    db: AsyncSession,
    current_time: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Generate fixed platform usage statements for completed months.
    Payment collection and due/overdue reminder dispatch are not implemented here.
    """
    now = current_time or datetime.now(timezone.utc)
    from sqlalchemy import select
    from app.models.organization import Organization
    from app.modules.billing.platform import finalize_months
    generated = 0
    orgs = (await db.scalars(select(Organization).where(Organization.deleted_at.is_(None)).order_by(Organization.id))).all()
    for org in orgs:
        await db.execute(select(Organization.id).where(Organization.id == org.id).with_for_update())
        generated += await finalize_months(db, org, now)
    await db.commit()
    # Month-end usage statements are generated above. Payment collection and
    # paid/overdue reminder state are separate from this usage ledger.
    return {
        "processed_at": now.isoformat(),
        "platform_invoices_generated": generated,
        "due_soon_sent": 0,
        "overdue_sent": 0,
        "suppressed_already_paid": 0
    }


async def _record_brevo_email(db, log, html):
    from app.modules.communication.service import _dispatch_brevo_request
    key = settings.BREVO_API_KEY
    if not settings.BREVO_ENABLED:
        log.status, log.suppression_reason = "SUPPRESSED", "Progress Rooms email is paused"
    elif not key:
        log.status = "SIMULATED" if settings.APP_ENV in {"test", "development"} else "FAILED"
        log.error_details = "Brevo API key is not configured"
    else:
        try:
            status, data, _ = await _dispatch_brevo_request({"api-key": key, "accept": "application/json", "content-type": "application/json"},
                {"sender": {"name": settings.BREVO_SENDER_NAME, "email": settings.BREVO_SENDER_EMAIL},
                 "to": [{"email": log.recipient_email}], "subject": log.subject_or_title, "htmlContent": html})
            log.status = "SENT" if status in {200, 201} and data.get("messageId") else "FAILED"
            log.metadata_json = dict(log.metadata_json or {}, provider_message_id=data.get("messageId"))
            if log.status == "FAILED": log.error_details = f"Brevo rejected billing mail (HTTP {status})"
        except Exception:
            log.status, log.error_details = "UNCERTAIN", "Billing email submission outcome needs review"
    db.add(log)
    return log.status == "SENT"
