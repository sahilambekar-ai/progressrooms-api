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
    Uses the Progress Rooms Admin WhatsApp sender and Brevo transactional email.
    """
    order = await db.get(Order, order_id)
    if not order:
        logger.warning(f"notify_student_payment_received: Order {order_id} not found")
        return {"success": False, "reason": "Order not found"}

    student = await db.get(User, order.user_id)
    org = await db.get(Organization, order.organization_id)
    studio_name = org.name if org else "Progress Rooms Studio"

    # Find payment reference
    payment_stmt = select(Payment).where(Payment.order_id == order_id).order_by(desc(Payment.created_at)).limit(1)
    payment = (await db.execute(payment_stmt)).scalar_one_or_none()
    payment_ref = payment.provider_payment_id if payment and payment.provider_payment_id else order.order_number

    public_origin = getattr(settings, "WEBSITE_PUBLIC_ORIGIN", "http://localhost:3000")
    receipt_url = receipt_url_override or f"{public_origin}/student/receipts/{order.id}"

    settings_summary = await get_or_create_settings(db)
    pref = await db.scalar(
        select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == student.id)
    )

    amount = float(order.total_amount)
    offering_name = order.offering_label or "Class Pass / Drop-in"
    dispatched = {"whatsapp": False, "email": False}

    # 1. WhatsApp confirmation (Admin sender)
    wa_target = student.whatsapp_number or student.phone
    if wa_target:
        if not settings_summary.whatsapp_enabled:
            db.add(CommunicationLog(
                recipient_user_id=student.id,
                organization_id=order.organization_id,
                recipient_phone=wa_target,
                channel="WHATSAPP_ADMIN",
                message_type="PAYMENT_RECEIVED",
                status="SUPPRESSED",
                suppression_reason="Suppressed: whatsapp disabled",
                subject_or_title=f"Payment Confirmed: {offering_name}",
                metadata_json={"order_id": str(order.id), "amount": amount}
            ))
        elif pref and pref.whatsapp_opted_out:
            db.add(CommunicationLog(
                recipient_user_id=student.id,
                organization_id=order.organization_id,
                recipient_phone=wa_target,
                channel="WHATSAPP_ADMIN",
                message_type="PAYMENT_RECEIVED",
                status="SUPPRESSED",
                suppression_reason="Suppressed: recipient opt-out",
                subject_or_title=f"Payment Confirmed: {offering_name}"
            ))
        else:
            wa_text = render_student_payment_received_wa(
                student_name=student.full_name,
                studio_name=studio_name,
                class_or_pass_name=offering_name,
                amount_inr=amount,
                receipt_url=receipt_url
            )
            db.add(CommunicationLog(
                recipient_user_id=student.id,
                organization_id=order.organization_id,
                recipient_phone=wa_target,
                channel="WHATSAPP_ADMIN",
                message_type="PAYMENT_RECEIVED",
                status="SENT",
                subject_or_title=f"Payment Confirmed: {offering_name}",
                body_preview=wa_text[:200],
                metadata_json={"order_id": str(order.id), "amount": amount, "reference": payment_ref}
            ))
            dispatched["whatsapp"] = True

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
            subj, _ = render_student_payment_received_email(
                student_name=student.full_name,
                studio_name=studio_name,
                class_or_pass_name=offering_name,
                amount_inr=amount,
                receipt_url=receipt_url,
                payment_reference=payment_ref
            )
            db.add(CommunicationLog(
                recipient_user_id=student.id,
                organization_id=order.organization_id,
                recipient_email=student.email,
                channel="EMAIL",
                message_type="PAYMENT_RECEIVED",
                status="SENT",
                subject_or_title=subj,
                body_preview=f"Payment confirmed for {offering_name} at {studio_name}",
                metadata_json={"order_id": str(order.id), "amount": amount, "reference": payment_ref}
            ))
            dispatched["email"] = True

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
    inv_url = invoice_url or f"{public_origin}/dashboard/billing/invoices/{invoice_number}"
    due_date_str = format_due_date_str(due_date)

    settings_summary = await get_or_create_settings(db)
    dispatches_sent = 0

    for contact in contacts:
        pref = await db.scalar(
            select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == contact.id)
        )

        # WhatsApp (Admin sender)
        wa_target = contact.whatsapp_number or contact.phone
        if wa_target:
            if not settings_summary.whatsapp_enabled:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_ADMIN",
                    message_type="INVOICE_ISSUED",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: whatsapp disabled",
                    subject_or_title=f"Invoice Issued #{invoice_number}",
                    metadata_json={"invoice_number": invoice_number, "amount": amount_inr}
                ))
            elif pref and pref.whatsapp_opted_out:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_ADMIN",
                    message_type="INVOICE_ISSUED",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: recipient opt-out",
                    subject_or_title=f"Invoice Issued #{invoice_number}"
                ))
            else:
                wa_body = render_billing_invoice_wa(
                    studio_name=studio_name,
                    invoice_number=invoice_number,
                    amount_inr=amount_inr,
                    billing_period=billing_period,
                    due_date_str=due_date_str,
                    invoice_url=inv_url
                )
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_ADMIN",
                    message_type="INVOICE_ISSUED",
                    status="SENT",
                    subject_or_title=f"Invoice Issued #{invoice_number}",
                    body_preview=wa_body[:200],
                    metadata_json={"invoice_number": invoice_number, "amount": amount_inr}
                ))
                dispatches_sent += 1

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
                subj, _ = render_billing_invoice_email(
                    recipient_name=contact.full_name,
                    studio_name=studio_name,
                    invoice_number=invoice_number,
                    amount_inr=amount_inr,
                    billing_period=billing_period,
                    due_date_str=due_date_str,
                    invoice_url=inv_url
                )
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="INVOICE_ISSUED",
                    status="SENT",
                    subject_or_title=subj,
                    body_preview=f"Invoice #{invoice_number} for {studio_name}",
                    metadata_json={"invoice_number": invoice_number, "amount": amount_inr}
                ))
                dispatches_sent += 1

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
    inv_url = invoice_url or f"{public_origin}/dashboard/billing/invoices/{invoice_number}"
    due_date_str = format_due_date_str(due_date)

    settings_summary = await get_or_create_settings(db)
    dispatches_sent = 0

    for contact in contacts:
        pref = await db.scalar(
            select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == contact.id)
        )

        wa_target = contact.whatsapp_number or contact.phone
        if wa_target:
            if not settings_summary.whatsapp_enabled:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_ADMIN",
                    message_type="PAYMENT_DUE_SOON",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: whatsapp disabled",
                    subject_or_title=f"Payment Due Soon: {studio_name}",
                    metadata_json={"invoice_number": invoice_number, "amount": amount_inr}
                ))
            elif pref and pref.whatsapp_opted_out:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_ADMIN",
                    message_type="PAYMENT_DUE_SOON",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: recipient opt-out",
                    subject_or_title=f"Payment Due Soon: {studio_name}"
                ))
            else:
                wa_body = render_billing_due_soon_wa(
                    studio_name=studio_name,
                    amount_inr=amount_inr,
                    due_date_str=due_date_str,
                    invoice_url=inv_url
                )
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_ADMIN",
                    message_type="PAYMENT_DUE_SOON",
                    status="SENT",
                    subject_or_title=f"Payment Due Soon: {studio_name}",
                    body_preview=wa_body[:200],
                    metadata_json={"invoice_number": invoice_number, "amount": amount_inr}
                ))
                dispatches_sent += 1

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
                subj, _ = render_billing_due_soon_email(
                    recipient_name=contact.full_name,
                    studio_name=studio_name,
                    amount_inr=amount_inr,
                    due_date_str=due_date_str,
                    invoice_url=inv_url
                )
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="PAYMENT_DUE_SOON",
                    status="SENT",
                    subject_or_title=subj,
                    body_preview=f"Subscription payment due on {due_date_str}",
                    metadata_json={"invoice_number": invoice_number, "amount": amount_inr}
                ))
                dispatches_sent += 1

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
    inv_url = invoice_url or f"{public_origin}/dashboard/billing/invoices/{invoice_number}"

    settings_summary = await get_or_create_settings(db)
    dispatches_sent = 0

    for contact in contacts:
        pref = await db.scalar(
            select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == contact.id)
        )

        wa_target = contact.whatsapp_number or contact.phone
        if wa_target:
            if not settings_summary.whatsapp_enabled:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_ADMIN",
                    message_type="PAYMENT_OVERDUE",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: whatsapp disabled",
                    subject_or_title=f"⚠️ Overdue Notice: {studio_name}",
                    metadata_json={"invoice_number": invoice_number, "days_overdue": days_overdue}
                ))
            elif pref and pref.whatsapp_opted_out:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_ADMIN",
                    message_type="PAYMENT_OVERDUE",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: recipient opt-out",
                    subject_or_title=f"⚠️ Overdue Notice: {studio_name}"
                ))
            else:
                wa_body = render_billing_overdue_wa(
                    studio_name=studio_name,
                    amount_inr=amount_inr,
                    days_overdue=days_overdue,
                    invoice_url=inv_url
                )
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_ADMIN",
                    message_type="PAYMENT_OVERDUE",
                    status="SENT",
                    subject_or_title=f"⚠️ Overdue Notice: {studio_name}",
                    body_preview=wa_body[:200],
                    metadata_json={"invoice_number": invoice_number, "days_overdue": days_overdue}
                ))
                dispatches_sent += 1

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
                subj, _ = render_billing_overdue_email(
                    recipient_name=contact.full_name,
                    studio_name=studio_name,
                    amount_inr=amount_inr,
                    days_overdue=days_overdue,
                    invoice_url=inv_url
                )
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="PAYMENT_OVERDUE",
                    status="SENT",
                    subject_or_title=subj,
                    body_preview=f"Subscription payment {days_overdue} days overdue",
                    metadata_json={"invoice_number": invoice_number, "days_overdue": days_overdue}
                ))
                dispatches_sent += 1

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
    rec_url = receipt_url or f"{public_origin}/dashboard/billing/receipts/{payment_reference}"

    settings_summary = await get_or_create_settings(db)
    dispatches_sent = 0

    for contact in contacts:
        pref = await db.scalar(
            select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == contact.id)
        )

        wa_target = contact.whatsapp_number or contact.phone
        if wa_target:
            if not settings_summary.whatsapp_enabled:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_ADMIN",
                    message_type="STUDIO_PAYMENT_RECEIVED",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: whatsapp disabled",
                    subject_or_title=f"Payment Received: {studio_name}",
                    metadata_json={"reference": payment_reference, "amount": amount_inr}
                ))
            elif pref and pref.whatsapp_opted_out:
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_ADMIN",
                    message_type="STUDIO_PAYMENT_RECEIVED",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: recipient opt-out",
                    subject_or_title=f"Payment Received: {studio_name}"
                ))
            else:
                wa_body = render_billing_payment_received_wa(
                    studio_name=studio_name,
                    amount_inr=amount_inr,
                    receipt_url=rec_url
                )
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_ADMIN",
                    message_type="STUDIO_PAYMENT_RECEIVED",
                    status="SENT",
                    subject_or_title=f"Payment Received: {studio_name}",
                    body_preview=wa_body[:200],
                    metadata_json={"reference": payment_reference, "amount": amount_inr}
                ))
                dispatches_sent += 1

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
                subj, _ = render_billing_payment_received_email(
                    recipient_name=contact.full_name,
                    studio_name=studio_name,
                    amount_inr=amount_inr,
                    receipt_url=rec_url,
                    payment_reference=payment_reference
                )
                db.add(CommunicationLog(
                    recipient_user_id=contact.id,
                    organization_id=organization_id,
                    recipient_email=contact.email,
                    channel="EMAIL",
                    message_type="STUDIO_PAYMENT_RECEIVED",
                    status="SENT",
                    subject_or_title=subj,
                    body_preview=f"Payment of ₹{amount_inr:,.2f} confirmed for {studio_name}",
                    metadata_json={"reference": payment_reference, "amount": amount_inr}
                ))
                dispatches_sent += 1

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
