import math
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select, func, desc
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.user import User
from app.models.communication import CommunicationLog, CommunicationChannelAuditLog
from app.modules.auth.dependencies import require_superadmin
from app.modules.communication.schemas import (
    CommunicationSettingsResponse,
    CommunicationSettingsUpdate,
    CommunicationTestPayload,
    CommunicationTestResponse,
    CommunicationLogListResponse,
    CommunicationLogItem,
    CommunicationAuditLogItem,
    StudioBudgetResponse,
    StudioBudgetUpdate,
    EngineCycleResponse,
)
from app.modules.communication.service import (
    get_settings_summary,
    update_settings,
    send_test_message,
    get_all_studio_budgets,
    update_studio_budget,
)
from app.modules.communication.reminder_engine import (
    dispatch_pending_reminders,
    process_debounced_material_corrections,
)
from app.modules.communication.billing_engine import (
    process_billing_reminders,
)


router = APIRouter(prefix="/admin/communication", tags=["Platform Communication Channels"])


@router.get("/settings", response_model=CommunicationSettingsResponse)
async def get_communication_settings(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Returns platform-wide communication channel switches, health indicators,
    and update metadata. Scope applies to all studios.
    """
    return await get_settings_summary(db)


@router.patch("/settings", response_model=CommunicationSettingsResponse)
async def patch_communication_settings(
    payload: CommunicationSettingsUpdate,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Toggles operational communication switches for WhatsApp and/or Email.
    Enforces that only Super Admin can alter platform-wide delivery switches.
    Every change is recorded in the channel audit log.
    """
    return await update_settings(
        db=db,
        admin_user=admin,
        whatsapp_enabled=payload.whatsapp_enabled,
        email_enabled=payload.email_enabled,
        notes=payload.notes
    )


@router.post("/test", response_model=CommunicationTestResponse)
async def trigger_communication_test(
    payload: CommunicationTestPayload,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Dispatches a clearly labelled operational notification test to an authorized recipient.
    Obeys the backend channel switches: if the target channel is switched off,
    the message is suppressed and recorded with a reason (e.g. 'Suppressed: email disabled').
    """
    return await send_test_message(db=db, admin_user=admin, payload=payload)


@router.get("/history", response_model=CommunicationLogListResponse)
async def get_communication_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    channel: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Paginated communication activity history, showing sent, failed, and suppressed
    messages with their reasons.
    """
    stmt = (
        select(CommunicationLog)
        .options(selectinload(CommunicationLog.recipient_user))
    )

    if channel and channel.upper() != "ALL":
        stmt = stmt.where(CommunicationLog.channel == channel.upper())

    if status_filter and status_filter.upper() != "ALL":
        stmt = stmt.where(CommunicationLog.status == status_filter.upper())

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_items = (await db.scalar(count_stmt)) or 0
    total_pages = max(1, math.ceil(total_items / page_size))

    offset = (page - 1) * page_size
    stmt = stmt.order_by(desc(CommunicationLog.created_at)).offset(offset).limit(page_size)
    result = await db.execute(stmt)
    records = result.scalars().all()

    items = [
        CommunicationLogItem(
            id=str(r.id),
            recipient_email=r.recipient_email,
            recipient_phone=r.recipient_phone,
            recipient_name=r.recipient_user.full_name if r.recipient_user else None,
            channel=r.channel,
            message_type=r.message_type,
            status=r.status,
            suppression_reason=r.suppression_reason,
            subject_or_title=r.subject_or_title,
            body_preview=r.body_preview,
            created_at=r.created_at.isoformat() if r.created_at else "",
            error_details=r.error_details
        )
        for r in records
    ]

    return CommunicationLogListResponse(
        items=items,
        total_items=total_items,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )


@router.get("/audit-logs", response_model=list[CommunicationAuditLogItem])
async def get_communication_audit_logs(
    limit: int = Query(50, ge=1, le=100),
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Returns the audit trail of who changed platform communication channel switches and when.
    """
    stmt = (
        select(CommunicationChannelAuditLog)
        .order_by(desc(CommunicationChannelAuditLog.changed_at))
        .limit(limit)
    )
    result = await db.execute(stmt)
    logs = result.scalars().all()

    return [
        CommunicationAuditLogItem(
            id=str(log.id),
            channel=log.channel,
            previous_state=log.previous_state,
            new_state=log.new_state,
            changed_by_email=log.changed_by_email,
            changed_at=log.changed_at.isoformat() if log.changed_at else "",
            notes=log.notes
        )
        for log in logs
    ]


@router.get("/budgets", response_model=list[StudioBudgetResponse])
async def list_studio_budgets(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Returns monthly communication allowances, current usage (INR), and message counts
    for each active studio.
    """
    return await get_all_studio_budgets(db)


@router.patch("/budgets/{organization_id}", response_model=StudioBudgetResponse)
async def patch_studio_budget(
    organization_id: str,
    payload: StudioBudgetUpdate,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Updates the monthly rupee allowance or overage policy for a studio.
    Policies: RESERVE_CRITICAL, STRICT_SUPPRESS, ALLOW_OVERAGE.
    """
    import uuid
    try:
        org_uuid = uuid.UUID(organization_id)
    except ValueError:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid organization UUID")

    return await update_studio_budget(
        db=db,
        organization_id=org_uuid,
        monthly_allowance_inr=payload.monthly_allowance_inr,
        overage_policy=payload.overage_policy
    )


@router.post("/run-engine-cycle", response_model=EngineCycleResponse)
async def trigger_engine_cycle(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """
    Triggers an immediate cycle of the ProgressRooms notification engine:
    1. Dispatches all due class reminders (REMINDER_2H, REMINDER_15M).
    2. Processes any expired 2-minute debounced material schedule corrections.
    3. Evaluates studio subscription billing due dates and stops pending reminders if already paid.
    Returns a structured summary of dispatches and suppressions.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    reminders_count = await dispatch_pending_reminders(db=db, as_of_time=now)
    corrections_count = await process_debounced_material_corrections(db=db, as_of_time=now)
    billing_report = await process_billing_reminders(db=db, current_time=now)

    return EngineCycleResponse(
        timestamp=now.isoformat(),
        reminders_dispatched=reminders_count,
        corrections_dispatched=corrections_count,
        billing_summary=billing_report,
        details=(
            f"Engine cycle completed: {reminders_count} reminder(s) dispatched, "
            f"{corrections_count} debounced correction(s) dispatched."
        )
    )

