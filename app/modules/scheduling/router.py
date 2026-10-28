import uuid
from datetime import datetime, timezone
from pydantic import BaseModel, Field, AwareDatetime, model_validator
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
from app.modules.organizations.zoom_service import get_or_create_occurrence_meeting, ZoomAuthError

router = APIRouter(prefix="/organizations/{org_id}/occurrences", tags=["Scheduling & Calendar"])

class ReschedulePayload(BaseModel):
    new_start_at: AwareDatetime
    new_end_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def valid_time_range(self):
        if self.new_end_at <= self.new_start_at:
            raise ValueError("End time must be after start time")
        return self

class CancelPayload(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)

@router.get("")
async def get_calendar_occurrences(
    org_id: uuid.UUID,
    start_date: AwareDatetime = Query(...),
    end_date: AwareDatetime = Query(...),
    tenant: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member),
    db: AsyncSession = Depends(get_db)
):
    if end_date < start_date:
        raise HTTPException(422, "End date must be on or after start date")
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
            "description": o.session.full_description if o.session else None,
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
    from app.modules.zoom.service import lock_studio
    await lock_studio(db, org_id)
    stmt = select(ClassOccurrence).where(ClassOccurrence.id == occurrence_id, ClassOccurrence.organization_id == org_id).with_for_update().execution_options(populate_existing=True)
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

    if payload.new_end_at <= payload.new_start_at:
        raise HTTPException(422, "Class end must be after its start")
    from app.modules.zoom.service import lock_studio
    await lock_studio(db, org_id)
    from app.modules.zoom.meetings import synchronize
    await synchronize(db, occ, "reschedule", start=payload.new_start_at, end=payload.new_end_at)
    old_start = occ.actual_start_at
    occ.actual_start_at = payload.new_start_at
    occ.actual_end_at = payload.new_end_at
    occ.status = OccurrenceStatus.RESCHEDULED.value

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
    from app.modules.zoom.service import lock_studio
    await lock_studio(db, org_id)
    stmt = select(ClassOccurrence).where(ClassOccurrence.id == occurrence_id, ClassOccurrence.organization_id == org_id).with_for_update().execution_options(populate_existing=True)
    res = await db.execute(stmt)
    occ = res.scalar_one_or_none()
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Occurrence not found")

    if occ.status == OccurrenceStatus.CANCELLED.value:
        return {"message": "Occurrence already cancelled"}
    from app.modules.zoom.meetings import synchronize
    await synchronize(db, occ, "cancel")
    occ.status = OccurrenceStatus.CANCELLED.value
    occ.cancellation_reason = payload.reason

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
    occ = await accessible_occurrence(db, org_id, occurrence_id, tenant[1], lock=False)
    if occ.status == "CANCELLED":
        raise HTTPException(409, "This class is no longer joinable")

    try:
        meeting_data = await get_or_create_occurrence_meeting(db, occ, tenant[0], is_host=True, actor_user_id=tenant[1].user_id if tenant[1] else None)
        return {
            "meeting_url": meeting_data["target_url"],
            "join_url": meeting_data["join_url"],
            "host_url": meeting_data["host_url"],
            "provider": meeting_data["provider"],
            "is_simulated": meeting_data.get("is_simulated", False),
            "host_email": meeting_data.get("host_email"),
            "can_start": meeting_data.get("can_start", False),
            "restricted_settings": meeting_data.get("restricted_settings", []),
            "start_responsibility": meeting_data.get("start_responsibility")
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
    from app.modules.zoom.meetings import host_for_occurrence
    can_start = {}
    for occurrence in selected:
        try:
            connection = await host_for_occurrence(db, occurrence)
            host_member = await db.get(OrganizationMember, connection.member_id)
            can_start[occurrence.id] = bool(member and host_member and host_member.user_id == member.user_id)
        except HTTPException:
            can_start[occurrence.id] = False
    return {"day": "today" if today else "tomorrow", "timezone": org.timezone, "classes": [dict(
        id=str(o.id), name=o.session.name, description=o.session.full_description, start_at=o.actual_start_at.isoformat(), end_at=o.actual_end_at.isoformat(),
        status=o.status, can_start=can_start.get(o.id, False), meeting_url=o.meeting_url, host_email=o.zoom_host_email, zoom_status=o.zoom_provisioning_status,
        is_current=ensure_utc(o.actual_start_at) <= now < ensure_utc(o.actual_end_at)) for o in selected]}


@router.post("/{occurrence_id}/send-invite", status_code=202)
async def send_occurrence_invites(org_id: uuid.UUID, occurrence_id: uuid.UUID,
        tenant=Depends(require_org_role([UserRole.STUDIO, UserRole.TEACHER])), db: AsyncSession = Depends(get_db)):
    from app.modules.communication.reminder_engine import request_meeting_invites
    occurrence = await accessible_occurrence(db, org_id, occurrence_id, tenant[1], lock=True)
    return await request_meeting_invites(db, occurrence)
