import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy import select, desc, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from app.core.config import settings
from app.models.user import User
from app.models.organization import Organization
from app.models.communication import (
    PlatformCommunicationSetting,
    CommunicationChannelAuditLog,
    CommunicationLog,
    StudioCommunicationBudget,
)
from app.modules.communication.schemas import (
    ChannelHealthStatus,
    ConnectionStatusResponse,
    CommunicationSettingsResponse,
    CommunicationTestPayload,
    CommunicationTestResponse,
    StudioBudgetResponse,
)

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
logger = logging.getLogger("progressrooms.communication")

COST_WHATSAPP_MESSAGE_INR = 0.75
COST_EMAIL_MESSAGE_INR = 0.05
CRITICAL_MESSAGE_TYPES = {"CLASS_CANCELLATION", "CORRECTION_CONSOLIDATED", "CLASS_CORRECTION", "URGENT_SCHEDULE_CHANGE"}


async def _dispatch_brevo_request(headers: dict, payload: dict) -> tuple[int, dict, str]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(BREVO_API_URL, headers=headers, json=payload)
        json_data = resp.json() if resp.content and resp.status_code in [200, 201] else {}
        return resp.status_code, json_data, resp.text



async def get_or_create_settings(db: AsyncSession) -> PlatformCommunicationSetting:
    """
    Retrieves the singleton platform-wide communication setting,
    or creates it with defaults (both channels enabled).
    """
    stmt = (
        select(PlatformCommunicationSetting)
        .options(selectinload(PlatformCommunicationSetting.updated_by_user))
        .limit(1)
    )
    result = await db.execute(stmt)
    setting = result.scalar_one_or_none()

    if not setting:
        setting = PlatformCommunicationSetting(
            whatsapp_enabled=True,
            email_enabled=True,
        )
        db.add(setting)
        await db.commit()
        await db.refresh(setting)

    return setting


def get_connection_status() -> ConnectionStatusResponse:
    """
    Inspects provider credentials and effective status for all three platform channels:
    - Brevo Email API
    - ProgressRooms Admin WhatsApp Sender
    - ProgressRooms Classes WhatsApp Sender
    """
    brevo_key = getattr(settings, "BREVO_API_KEY", "")
    brevo_configured = bool(brevo_key and len(brevo_key.strip()) > 5)
    brevo_sender = getattr(settings, "BREVO_SENDER_EMAIL", "sahilambekar.dev@gmail.com")

    email_status = ChannelHealthStatus(
        configured=brevo_configured,
        status="CONNECTED" if brevo_configured else "NOT_CONFIGURED",
        details="Brevo REST API v3 configured" if brevo_configured else "BREVO_API_KEY is not configured",
        sender_identity=f"{getattr(settings, 'BREVO_SENDER_NAME', 'ProgressRooms')} <{brevo_sender}>"
    )

    # WhatsApp Admin Sender (ProgressRooms Admin)
    wa_admin_num = getattr(settings, "WHATSAPP_ADMIN_NUMBER", "+91 98765 00001")
    wa_token = getattr(settings, "WHATSAPP_API_TOKEN", "")
    wa_admin_configured = bool(wa_token and len(wa_token.strip()) > 5)

    wa_admin_status = ChannelHealthStatus(
        configured=wa_admin_configured,
        status="CONNECTED" if wa_admin_configured else "READY_FOR_SETUP",
        details="Meta WhatsApp Cloud API configured for Admin sender" if wa_admin_configured else "Meta Cloud API credentials pending; ready for verification",
        sender_identity=f"ProgressRooms Admin ({wa_admin_num})"
    )

    # WhatsApp Classes Sender (ProgressRooms Classes)
    wa_classes_num = getattr(settings, "WHATSAPP_CLASSES_NUMBER", "+91 98765 00002")
    wa_classes_configured = bool(wa_token and len(wa_token.strip()) > 5)

    wa_classes_status = ChannelHealthStatus(
        configured=wa_classes_configured,
        status="CONNECTED" if wa_classes_configured else "READY_FOR_SETUP",
        details="Meta WhatsApp Cloud API configured for Classes sender" if wa_classes_configured else "Meta Cloud API credentials pending; ready for verification",
        sender_identity=f"ProgressRooms Classes ({wa_classes_num})"
    )

    return ConnectionStatusResponse(
        email=email_status,
        whatsapp_admin=wa_admin_status,
        whatsapp_classes=wa_classes_status,
    )


async def get_settings_summary(db: AsyncSession) -> CommunicationSettingsResponse:
    setting = await get_or_create_settings(db)
    health = get_connection_status()

    both_off = (not setting.whatsapp_enabled) and (not setting.email_enabled)

    return CommunicationSettingsResponse(
        whatsapp_enabled=setting.whatsapp_enabled,
        email_enabled=setting.email_enabled,
        both_channels_disabled=both_off,
        updated_at=setting.updated_at,
        updated_by_user_id=str(setting.updated_by_user_id) if setting.updated_by_user_id else None,
        updated_by_email=setting.updated_by_user.email if setting.updated_by_user else None,
        scope="Applies to all studios",
        connection_status=health
    )


async def update_settings(
    db: AsyncSession,
    admin_user: User,
    whatsapp_enabled: bool | None,
    email_enabled: bool | None,
    notes: str | None = None
) -> CommunicationSettingsResponse:
    setting = await get_or_create_settings(db)
    now = datetime.now(timezone.utc)

    # Record WhatsApp toggle audit log
    if whatsapp_enabled is not None and whatsapp_enabled != setting.whatsapp_enabled:
        audit_wa = CommunicationChannelAuditLog(
            channel="WHATSAPP",
            previous_state=setting.whatsapp_enabled,
            new_state=whatsapp_enabled,
            changed_by_user_id=admin_user.id,
            changed_by_email=admin_user.email,
            changed_at=now,
            notes=notes or f"WhatsApp operational notifications turned {'ON' if whatsapp_enabled else 'OFF'}"
        )
        db.add(audit_wa)
        setting.whatsapp_enabled = whatsapp_enabled

    # Record Email toggle audit log
    if email_enabled is not None and email_enabled != setting.email_enabled:
        audit_em = CommunicationChannelAuditLog(
            channel="EMAIL",
            previous_state=setting.email_enabled,
            new_state=email_enabled,
            changed_by_user_id=admin_user.id,
            changed_by_email=admin_user.email,
            changed_at=now,
            notes=notes or f"Email operational notifications turned {'ON' if email_enabled else 'OFF'}"
        )
        db.add(audit_em)
        setting.email_enabled = email_enabled

    setting.updated_by_user_id = admin_user.id
    setting.updated_at = now
    await db.commit()
    await db.refresh(setting)

    return await get_settings_summary(db)


async def send_test_message(
    db: AsyncSession,
    admin_user: User,
    payload: CommunicationTestPayload
) -> CommunicationTestResponse:
    """
    Sends a clearly labelled operational test notification to verify channel routing.
    Enforces backend channel switches; if channel is disabled, logs suppression.
    """
    setting = await get_or_create_settings(db)
    target_channel = payload.channel.upper().strip()
    recipient = payload.recipient.strip()

    subject = "[TEST] ProgressRooms Operational Notification Test"
    custom_note = payload.message.strip() if payload.message else "Routine channel verification test."
    body_text = (
        f"[TEST MESSAGE - ProgressRooms Notification Engine]\n\n"
        f"Channel: {target_channel}\n"
        f"Recipient: {recipient}\n"
        f"Dispatched by: {admin_user.full_name} ({admin_user.email})\n"
        f"Scope: Applies to all studios\n"
        f"Note: {custom_note}\n\n"
        f"This test confirms operational routing between ProgressRooms and delivery providers."
    )

    # 1. Enforcement of channel toggles
    if target_channel == "EMAIL":
        if not setting.email_enabled:
            # Log suppression
            log_entry = CommunicationLog(
                recipient_user_id=admin_user.id,
                recipient_email=recipient,
                channel="EMAIL",
                message_type="TEST_MESSAGE",
                status="SUPPRESSED",
                suppression_reason="Suppressed: email disabled",
                subject_or_title=subject,
                body_preview=body_text[:200],
                metadata_json={"test": True, "initiated_by": admin_user.email}
            )
            db.add(log_entry)
            await db.commit()
            return CommunicationTestResponse(
                success=False,
                channel=target_channel,
                recipient=recipient,
                status="SUPPRESSED",
                suppression_reason="Suppressed: email disabled",
                details="Email notifications are currently turned off at the platform level. Scope applies to all studios."
            )

        # Dispatch via Brevo
        api_key = getattr(settings, "BREVO_API_KEY", "")
        if api_key:
            headers = {
                "api-key": api_key,
                "accept": "application/json",
                "content-type": "application/json"
            }
            send_payload = {
                "sender": {
                    "name": getattr(settings, "BREVO_SENDER_NAME", "ProgressRooms"),
                    "email": getattr(settings, "BREVO_SENDER_EMAIL", "sahilambekar.dev@gmail.com")
                },
                "to": [{"email": recipient, "name": "Authorized Test Recipient"}],
                "subject": subject,
                "htmlContent": f"<div style='font-family:sans-serif;padding:20px;border:2px dashed #81A882;border-radius:8px;'><h3 style='color:#0C2417;'>🌿 ProgressRooms Notification Test</h3><p>{body_text.replace(chr(10), '<br>')}</p></div>"
            }
            try:
                status_code, json_data, text_data = await _dispatch_brevo_request(headers=headers, payload=send_payload)
                if status_code in [200, 201]:
                    msg_id = json_data.get("messageId", f"br_{uuid.uuid4().hex[:8]}")
                    log_entry = CommunicationLog(
                        recipient_user_id=admin_user.id,
                        recipient_email=recipient,
                        channel="EMAIL",
                        message_type="TEST_MESSAGE",
                        status="SENT",
                        subject_or_title=subject,
                        body_preview=body_text[:200],
                        metadata_json={"test": True, "message_id": msg_id}
                    )
                    db.add(log_entry)
                    await db.commit()
                    return CommunicationTestResponse(
                        success=True,
                        channel=target_channel,
                        recipient=recipient,
                        status="SENT",
                        message_id=msg_id,
                        details="Test email successfully dispatched via Brevo."
                    )
                else:
                    err_msg = resp.text
                    log_entry = CommunicationLog(
                        recipient_user_id=admin_user.id,
                        recipient_email=recipient,
                        channel="EMAIL",
                        message_type="TEST_MESSAGE",
                        status="FAILED",
                        error_details=err_msg,
                        subject_or_title=subject,
                        body_preview=body_text[:200]
                    )
                    db.add(log_entry)
                    await db.commit()
                    return CommunicationTestResponse(
                        success=False,
                        channel=target_channel,
                        recipient=recipient,
                        status="FAILED",
                        details=f"Brevo HTTP error: {err_msg}"
                    )
            except Exception as e:
                err_msg = str(e)
                log_entry = CommunicationLog(
                    recipient_user_id=admin_user.id,
                    recipient_email=recipient,
                    channel="EMAIL",
                    message_type="TEST_MESSAGE",
                    status="FAILED",
                    error_details=err_msg,
                    subject_or_title=subject,
                    body_preview=body_text[:200]
                )
                db.add(log_entry)
                await db.commit()
                return CommunicationTestResponse(
                    success=False,
                    channel=target_channel,
                    recipient=recipient,
                    status="FAILED",
                    details=f"Network exception: {err_msg}"
                )
        else:
            # Simulated send when no key set in local development
            msg_id = f"sim_email_{uuid.uuid4().hex[:8]}"
            log_entry = CommunicationLog(
                recipient_user_id=admin_user.id,
                recipient_email=recipient,
                channel="EMAIL",
                message_type="TEST_MESSAGE",
                status="SENT",
                subject_or_title=subject,
                body_preview=body_text[:200],
                metadata_json={"simulated": True, "message_id": msg_id}
            )
            db.add(log_entry)
            await db.commit()
            return CommunicationTestResponse(
                success=True,
                channel=target_channel,
                recipient=recipient,
                status="SENT",
                message_id=msg_id,
                details="Test email simulated in local development (BREVO_API_KEY is unset)."
            )

    elif target_channel in ["WHATSAPP_ADMIN", "WHATSAPP_CLASSES"]:
        if not setting.whatsapp_enabled:
            # Log suppression
            log_entry = CommunicationLog(
                recipient_user_id=admin_user.id,
                recipient_phone=recipient,
                channel=target_channel,
                message_type="TEST_MESSAGE",
                status="SUPPRESSED",
                suppression_reason="Suppressed: whatsapp disabled",
                subject_or_title=subject,
                body_preview=body_text[:200],
                metadata_json={"test": True, "initiated_by": admin_user.email}
            )
            db.add(log_entry)
            await db.commit()
            return CommunicationTestResponse(
                success=False,
                channel=target_channel,
                recipient=recipient,
                status="SUPPRESSED",
                suppression_reason="Suppressed: whatsapp disabled",
                details="WhatsApp notifications are currently turned off at the platform level. Scope applies to all studios."
            )

        # Simulated or Meta Cloud API dispatch
        msg_id = f"wa_test_{uuid.uuid4().hex[:8]}"
        log_entry = CommunicationLog(
            recipient_user_id=admin_user.id,
            recipient_phone=recipient,
            channel=target_channel,
            message_type="TEST_MESSAGE",
            status="SENT",
            subject_or_title=subject,
            body_preview=body_text[:200],
            metadata_json={"simulated": True, "sender_channel": target_channel, "message_id": msg_id}
        )
        db.add(log_entry)
        await db.commit()
        return CommunicationTestResponse(
            success=True,
            channel=target_channel,
            recipient=recipient,
            status="SENT",
            message_id=msg_id,
            details=f"Test message routed through ProgressRooms {target_channel.replace('_', ' ').title()}."
        )

    else:
        return CommunicationTestResponse(
            success=False,
            channel=target_channel,
            recipient=recipient,
            status="FAILED",
            details=f"Unsupported communication channel: {target_channel}"
        )


# ---------------------------------------------------------------------------
# Studio Communication Budget Management
# ---------------------------------------------------------------------------

async def get_or_create_studio_budget(
    db: AsyncSession,
    organization_id: uuid.UUID
) -> StudioCommunicationBudget:
    stmt = select(StudioCommunicationBudget).where(StudioCommunicationBudget.organization_id == organization_id)
    budget = (await db.execute(stmt)).scalar_one_or_none()
    if not budget:
        budget = StudioCommunicationBudget(
            organization_id=organization_id,
            monthly_allowance_inr=1000.0,
            current_month_usage_inr=0.0,
            current_month_message_count=0,
            overage_policy="RESERVE_CRITICAL",
            alert_level_sent=0,
        )
        db.add(budget)
        await db.flush()
    return budget


async def check_and_increment_studio_budget(
    db: AsyncSession,
    organization_id: uuid.UUID | None,
    channel: str,
    message_type: str
) -> tuple[bool, str | None]:
    """
    Evaluates whether an outbound message can be sent within the studio's monthly budget.
    Platform billing messages have organization_id=None and are paid directly by ProgressRooms platform.

    Returns:
        (True, None) if allowed and usage is recorded
        (False, reason_str) if suppressed due to over-budget policy
    """
    if organization_id is None:
        # Platform billing messages are accounted for separately from a studio's student-message allowance
        return True, None

    budget = await get_or_create_studio_budget(db, organization_id)

    unit_cost = COST_WHATSAPP_MESSAGE_INR if "WHATSAPP" in channel.upper() else COST_EMAIL_MESSAGE_INR
    projected_usage = budget.current_month_usage_inr + unit_cost

    # Check if budget is exceeded
    if projected_usage > budget.monthly_allowance_inr:
        if budget.overage_policy == "STRICT_SUPPRESS":
            return False, "Suppressed: studio communication budget exceeded"
        elif budget.overage_policy == "RESERVE_CRITICAL":
            if message_type not in CRITICAL_MESSAGE_TYPES:
                return False, "Suppressed: studio budget exceeded (reserved for critical)"
            # Critical messages (cancellations & urgent corrections) are allowed even over budget!

    # Record usage
    budget.current_month_usage_inr = round(projected_usage, 2)
    budget.current_month_message_count += 1

    # Check alert thresholds: 70%, 90%, 100%
    ratio = (budget.current_month_usage_inr / budget.monthly_allowance_inr) * 100.0 if budget.monthly_allowance_inr > 0 else 100.0
    if ratio >= 100 and budget.alert_level_sent < 100:
        budget.alert_level_sent = 100
        logger.warning(
            f"Studio {organization_id} reached 100% of communication allowance "
            f"(₹{budget.current_month_usage_inr}/₹{budget.monthly_allowance_inr})"
        )
    elif ratio >= 90 and budget.alert_level_sent < 90:
        budget.alert_level_sent = 90
        logger.info(f"Studio {organization_id} reached 90% of communication allowance")
    elif ratio >= 70 and budget.alert_level_sent < 70:
        budget.alert_level_sent = 70
        logger.info(f"Studio {organization_id} reached 70% of communication allowance")

    await db.flush()
    return True, None


async def update_studio_budget(
    db: AsyncSession,
    organization_id: uuid.UUID,
    monthly_allowance_inr: float | None = None,
    overage_policy: str | None = None
) -> StudioBudgetResponse:
    budget = await get_or_create_studio_budget(db, organization_id)
    if monthly_allowance_inr is not None:
        budget.monthly_allowance_inr = max(0.0, monthly_allowance_inr)
    if overage_policy is not None:
        valid_policies = {"RESERVE_CRITICAL", "STRICT_SUPPRESS", "ALLOW_OVERAGE"}
        if overage_policy.upper() in valid_policies:
            budget.overage_policy = overage_policy.upper()
    await db.commit()
    await db.refresh(budget)

    org = await db.get(Organization, organization_id)
    studio_name = org.name if org else "Unknown Studio"
    usage_pct = (budget.current_month_usage_inr / budget.monthly_allowance_inr) * 100.0 if budget.monthly_allowance_inr > 0 else 100.0
    remaining_inr = max(0.0, budget.monthly_allowance_inr - budget.current_month_usage_inr)
    est_msgs = int(remaining_inr / COST_WHATSAPP_MESSAGE_INR)

    return StudioBudgetResponse(
        organization_id=str(budget.organization_id),
        studio_name=studio_name,
        monthly_allowance_inr=budget.monthly_allowance_inr,
        current_month_usage_inr=budget.current_month_usage_inr,
        current_month_message_count=budget.current_month_message_count,
        overage_policy=budget.overage_policy,
        alert_level_sent=budget.alert_level_sent,
        usage_percentage=round(usage_pct, 1),
        estimated_messages_remaining=est_msgs
    )


async def get_all_studio_budgets(db: AsyncSession) -> list[StudioBudgetResponse]:
    orgs_res = await db.execute(select(Organization).where(Organization.status == "ACTIVE"))
    orgs = orgs_res.scalars().all()

    items: list[StudioBudgetResponse] = []
    for org in orgs:
        budget = await get_or_create_studio_budget(db, org.id)
        usage_pct = (budget.current_month_usage_inr / budget.monthly_allowance_inr) * 100.0 if budget.monthly_allowance_inr > 0 else 100.0
        remaining_inr = max(0.0, budget.monthly_allowance_inr - budget.current_month_usage_inr)
        est_msgs = int(remaining_inr / COST_WHATSAPP_MESSAGE_INR)
        items.append(
            StudioBudgetResponse(
                organization_id=str(org.id),
                studio_name=org.name,
                monthly_allowance_inr=budget.monthly_allowance_inr,
                current_month_usage_inr=budget.current_month_usage_inr,
                current_month_message_count=budget.current_month_message_count,
                overage_policy=budget.overage_policy,
                alert_level_sent=budget.alert_level_sent,
                usage_percentage=round(usage_pct, 1),
                estimated_messages_remaining=est_msgs
            )
        )
    return items

