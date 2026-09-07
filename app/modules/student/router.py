import uuid
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.commerce import SessionEnrollment
from app.models.schedule import ClassOccurrence
from app.models.session import Session
from app.models.organization import Organization, OrganizationMember
from app.models.user import User
from app.modules.auth.dependencies import get_current_user
from app.common.enums import EnrollmentStatus

router = APIRouter(prefix="/student", tags=["Student Portal"])

@router.get("/enrollments")
async def get_my_enrollments(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(SessionEnrollment).options(
        selectinload(SessionEnrollment.session).selectinload(Session.organization)
    ).where(
        SessionEnrollment.user_id == current_user.id
    ).order_by(SessionEnrollment.created_at.desc())

    res = await db.execute(stmt)
    enrollments = res.scalars().all()

    return [
        {
            "id": str(e.id),
            "session_id": str(e.session_id),
            "session_name": e.session.name if e.session else "Session",
            "organization_name": e.session.organization.name if e.session and e.session.organization else "Studio",
            "organization_slug": e.session.organization.slug if e.session and e.session.organization else "",
            "status": e.status,
            "valid_from": e.valid_from.isoformat(),
            "valid_until": e.valid_until.isoformat() if e.valid_until else None,
            "total_sessions": e.total_sessions,
            "sessions_attended": e.sessions_attended
        }
        for e in enrollments
    ]

@router.get("/upcoming-classes")
async def get_my_upcoming_classes(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=30)

    # Get active session IDs
    enr_stmt = select(SessionEnrollment.session_id).where(
        SessionEnrollment.user_id == current_user.id,
        SessionEnrollment.status == EnrollmentStatus.ACTIVE.value
    )
    enr_res = await db.execute(enr_stmt)
    active_session_ids = enr_res.scalars().all()

    if not active_session_ids:
        return []

    occ_stmt = select(ClassOccurrence).options(
        selectinload(ClassOccurrence.session).selectinload(Session.organization),
        selectinload(ClassOccurrence.instructor).selectinload(OrganizationMember.user)
    ).where(
        ClassOccurrence.session_id.in_(active_session_ids),
        ClassOccurrence.status == "SCHEDULED",
        ClassOccurrence.actual_start_at >= now,
        ClassOccurrence.actual_start_at <= future
    ).order_by(ClassOccurrence.actual_start_at.asc())

    occ_res = await db.execute(occ_stmt)
    occurrences = occ_res.scalars().all()

    return [
        {
            "id": str(o.id),
            "session_name": o.session.name if o.session else "Session",
            "organization_name": o.session.organization.name if o.session and o.session.organization else "Studio",
            "instructor_name": o.instructor.user.full_name if o.instructor and o.instructor.user else "Teacher",
            "start_at": o.actual_start_at.isoformat(),
            "end_at": o.actual_end_at.isoformat(),
            "can_join": (o.actual_start_at - now).total_seconds() <= 900 # within 15 mins
        }
        for o in occurrences
    ]

@router.get("/classes/{occurrence_id}/meeting-url")
async def get_class_meeting_url(
    occurrence_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    occ_stmt = select(ClassOccurrence).options(selectinload(ClassOccurrence.session)).where(ClassOccurrence.id == occurrence_id)
    occ_res = await db.execute(occ_stmt)
    occ = occ_res.scalar_one_or_none()
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found")

    # Check enrollment
    enr_stmt = select(SessionEnrollment).where(
        SessionEnrollment.session_id == occ.session_id,
        SessionEnrollment.user_id == current_user.id,
        SessionEnrollment.status == EnrollmentStatus.ACTIVE.value
    )
    enr_res = await db.execute(enr_stmt)
    enr = enr_res.scalar_one_or_none()
    if not enr:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not enrolled in this session")

    # Check 15-minute window
    now = datetime.now(timezone.utc)
    seconds_until_start = (occ.actual_start_at - now).total_seconds()
    if seconds_until_start > 900: # more than 15 mins before start
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Meeting link unlocks 15 minutes before scheduled class start time"
        )

    url = occ.meeting_url or occ.session.meeting_url or f"https://meet.progressrooms.com/{occ.id}"
    return {"meeting_url": url}
