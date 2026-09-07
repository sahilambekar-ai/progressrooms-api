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
from app.modules.organizations.dependencies import get_current_tenant_member, require_org_role
from app.common.enums import UserRole, OccurrenceStatus
from app.models.user import User
from app.modules.auth.dependencies import get_current_user

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
    stmt = select(ClassOccurrence).options(
        selectinload(ClassOccurrence.session),
        selectinload(ClassOccurrence.instructor).selectinload(OrganizationMember.user)
    ).where(
        ClassOccurrence.organization_id == org_id,
        ClassOccurrence.actual_start_at >= start_date,
        ClassOccurrence.actual_start_at <= end_date
    ).order_by(ClassOccurrence.actual_start_at.asc())

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
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.INSTRUCTOR])),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(ClassOccurrence).where(ClassOccurrence.id == occurrence_id, ClassOccurrence.organization_id == org_id)
    res = await db.execute(stmt)
    occ = res.scalar_one_or_none()
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Occurrence not found")

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
    await db.commit()

    return {"message": "Occurrence rescheduled successfully", "new_start_at": payload.new_start_at.isoformat()}

@router.post("/{occurrence_id}/cancel")
async def cancel_occurrence(
    org_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    payload: CancelPayload,
    current_user: User = Depends(get_current_user),
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.INSTRUCTOR])),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(ClassOccurrence).where(ClassOccurrence.id == occurrence_id, ClassOccurrence.organization_id == org_id)
    res = await db.execute(stmt)
    occ = res.scalar_one_or_none()
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Occurrence not found")

    occ.status = OccurrenceStatus.CANCELLED.value
    occ.cancellation_reason = payload.reason

    change = ClassOccurrenceChange(
        class_occurrence_id=occ.id,
        changed_by_user_id=current_user.id,
        change_type="CANCEL",
        reason=payload.reason
    )
    db.add(change)
    await db.commit()

    return {"message": "Occurrence cancelled successfully"}
