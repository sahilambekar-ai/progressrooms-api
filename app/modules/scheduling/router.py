import uuid
from datetime import datetime, timezone
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember
from app.models.schedule import ClassOccurrence, ClassOccurrenceChange
from app.models.session import Session
from .access import assigned_occurrence, accessible_occurrence
from app.modules.organizations.dependencies import get_current_tenant_member, require_org_role
from app.common.enums import UserRole, OccurrenceStatus
import json
from app.models.user import User
from app.modules.auth.dependencies import get_current_user
from app.modules.classes.service import auto_complete_expired_one_on_one_classes
from app.models.integration import OrganizationIntegration
from app.modules.organizations.zoom_service import get_or_create_occurrence_meeting, ZoomService, ZoomAuthError

router = APIRouter(prefix="/organizations/{org_id}/occurrences", tags=["Scheduling & Calendar"])

class ReschedulePayload(BaseModel):
    new_start_at: datetime
    new_end_at: datetime
    reason: str

class CancelPayload(BaseModel):
    reason: str

@router.get("")
async def get_calendar_occurrences(
    org_id: uuid.UUID,
    start_date: datetime = Query(...),
    end_date: datetime = Query(...),
    tenant: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member),
    db: AsyncSession = Depends(get_db)
):
    await auto_complete_expired_one_on_one_classes(db, org_id)

    stmt = select(ClassOccurrence).options(
        selectinload(ClassOccurrence.session),
        selectinload(ClassOccurrence.instructor).selectinload(OrganizationMember.user)
    ).where(
        ClassOccurrence.organization_id == org_id,
        ClassOccurrence.actual_start_at >= start_date,
        ClassOccurrence.actual_start_at <= end_date
    ).order_by(ClassOccurrence.actual_start_at.asc())

    if tenant[1] and tenant[1].role == "TEACHER":
        stmt = stmt.where(assigned_occurrence(tenant[1].id))
    res = await db.execute(stmt)
    occurrences = res.scalars().all()

    return [
        {
            "id": str(o.id),
            "session_id": str(o.session_id),
            "session_name": o.session.name if o.session else "Session",
            "instructor_name": o.instructor.user.full_name if o.instructor else "TBA",
            "start_at": o.actual_start_at.isoformat(),
            "end_at": o.actual_end_at.isoformat(),
            "status": o.status,
            "meeting_url": o.meeting_url
        }
        for o in occurrences
    ]

@router.post("/{occurrence_id}/reschedule")
async def reschedule_occurrence(
    org_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    payload: ReschedulePayload,
    current_user: User = Depends(get_current_user),
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.STUDIO])),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(ClassOccurrence).where(ClassOccurrence.id == occurrence_id, ClassOccurrence.organization_id == org_id)
    res = await db.execute(stmt)
    occ = res.scalar_one_or_none()
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Occurrence not found")

    if occ.instructor_id:
        from app.modules.scheduling.engine import SchedulingEngine
        if await SchedulingEngine.check_instructor_conflict(db, occ.instructor_id, payload.new_start_at, payload.new_end_at, exclude_occurrence_id=occ.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Teacher has a scheduling conflict during this new time slot."
            )

    old_start = occ.actual_start_at
    occ.actual_start_at = payload.new_start_at
    occ.actual_end_at = payload.new_end_at
    occ.status = OccurrenceStatus.RESCHEDULED.value

    # If meeting was already provisioned with Zoom, synchronize update to provider
    if occ.provider_meeting_id and occ.meeting_provider == "ZOOM":
        stmt_int = select(OrganizationIntegration).where(
            OrganizationIntegration.organization_id == org_id,
            OrganizationIntegration.provider == "ZOOM",
            OrganizationIntegration.is_active.is_(True)
        )
        integration = (await db.execute(stmt_int)).scalar_one_or_none()
        if integration and integration.credentials_enc:
            try:
                cred_data = json.loads(integration.credentials_enc)
                if not cred_data.get("is_simulated"):
                    t_info = await ZoomService.verify_and_obtain_token(
                        cred_data["account_id"], cred_data["client_id"], cred_data["client_secret"]
                    )
                    dur = max(15, int((payload.new_end_at - payload.new_start_at).total_seconds() / 60))
                    await ZoomService.update_meeting(
                        access_token=t_info["access_token"],
                        meeting_id=occ.provider_meeting_id,
                        start_time=payload.new_start_at,
                        duration_minutes=dur
                    )
            except Exception:
                pass

    change = ClassOccurrenceChange(
        class_occurrence_id=occ.id,
        changed_by_user_id=current_user.id,
        change_type="RESCHEDULE",
        previous_start_at=old_start,
        new_start_at=payload.new_start_at,
        reason=payload.reason
    )
    db.add(change)

    from app.modules.communication.reminder_engine import record_class_edit
    await record_class_edit(
        db=db,
        occurrence_id=occ.id,
        changed_fields={"actual_start_at": payload.new_start_at, "actual_end_at": payload.new_end_at},
        old_values={"actual_start_at": old_start}
    )

    await db.commit()

    return {"message": "Occurrence rescheduled successfully", "new_start_at": payload.new_start_at.isoformat()}

@router.post("/{occurrence_id}/cancel")
async def cancel_occurrence(
    org_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    payload: CancelPayload,
    current_user: User = Depends(get_current_user),
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.STUDIO])),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(ClassOccurrence).where(ClassOccurrence.id == occurrence_id, ClassOccurrence.organization_id == org_id)
    res = await db.execute(stmt)
    occ = res.scalar_one_or_none()
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Occurrence not found")

    occ.status = OccurrenceStatus.CANCELLED.value
    occ.cancellation_reason = payload.reason

    # Synchronize cancellation to Zoom if provider meeting exists
    if occ.provider_meeting_id and occ.meeting_provider == "ZOOM":
        stmt_int = select(OrganizationIntegration).where(
            OrganizationIntegration.organization_id == org_id,
            OrganizationIntegration.provider == "ZOOM",
            OrganizationIntegration.is_active.is_(True)
        )
        integration = (await db.execute(stmt_int)).scalar_one_or_none()
        if integration and integration.credentials_enc:
            try:
                cred_data = json.loads(integration.credentials_enc)
                if not cred_data.get("is_simulated"):
                    t_info = await ZoomService.verify_and_obtain_token(
                        cred_data["account_id"], cred_data["client_id"], cred_data["client_secret"]
                    )
                    await ZoomService.delete_meeting(
                        access_token=t_info["access_token"],
                        meeting_id=occ.provider_meeting_id
                    )
            except Exception:
                pass

    change = ClassOccurrenceChange(
        class_occurrence_id=occ.id,
        changed_by_user_id=current_user.id,
        change_type="CANCEL",
        reason=payload.reason
    )
    db.add(change)

    from app.modules.communication.reminder_engine import handle_class_cancellation
    await handle_class_cancellation(
        db=db,
        occurrence_id=occ.id,
        reason=payload.reason
    )

    await db.commit()

    return {"message": "Occurrence cancelled successfully"}

@router.post("/{occurrence_id}/join-link")
async def create_join_link(org_id: uuid.UUID, occurrence_id: uuid.UUID,
                           tenant=Depends(require_org_role([UserRole.STUDIO, UserRole.TEACHER])),
                           db: AsyncSession = Depends(get_db)):
    occ = await accessible_occurrence(db, org_id, occurrence_id, tenant[1], lock=True)
    if occ.status in {"CANCELLED", "COMPLETED"}:
        raise HTTPException(409, "This class is no longer joinable")

    try:
        meeting_data = await get_or_create_occurrence_meeting(db, occ, tenant[0], is_host=True)
        return {
            "meeting_url": meeting_data["target_url"],
            "join_url": meeting_data["join_url"],
            "host_url": meeting_data["host_url"],
            "provider": meeting_data["provider"],
            "is_simulated": meeting_data.get("is_simulated", False)
        }
    except ZoomAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

@router.get("/teacher-agenda")
async def teacher_agenda(org_id: uuid.UUID, tenant=Depends(require_org_role([UserRole.TEACHER, UserRole.STUDIO])),
                         db: AsyncSession = Depends(get_db)):
    await auto_complete_expired_one_on_one_classes(db, org_id)
    from datetime import timedelta, time
    from zoneinfo import ZoneInfo
    org, member = tenant
    now = datetime.now(timezone.utc)
    local_today = now.astimezone(ZoneInfo(org.timezone)).date()
    start = datetime.combine(local_today, time.min, ZoneInfo(org.timezone)).astimezone(timezone.utc)
    end = start + timedelta(days=2)
    stmt = select(ClassOccurrence).options(selectinload(ClassOccurrence.session)).where(
        ClassOccurrence.organization_id == org_id, ClassOccurrence.actual_start_at >= start,
        ClassOccurrence.actual_start_at < end, ClassOccurrence.status != "CANCELLED").order_by(ClassOccurrence.actual_start_at)
    if member and member.role == "TEACHER":
        stmt = stmt.where(assigned_occurrence(member.id))
    rows = (await db.execute(stmt)).scalars().all()
    from app.common.dates import ensure_utc
    today = [o for o in rows if ensure_utc(o.actual_start_at).astimezone(ZoneInfo(org.timezone)).date() == local_today]
    selected = today or rows
    return {"day": "today" if today else "tomorrow", "timezone": org.timezone, "classes": [dict(
        id=str(o.id), name=o.session.name, start_at=o.actual_start_at.isoformat(), end_at=o.actual_end_at.isoformat(),
        status=o.status, meeting_url=o.host_url or o.meeting_url,
        is_current=ensure_utc(o.actual_start_at) <= now < ensure_utc(o.actual_end_at)) for o in selected]}
