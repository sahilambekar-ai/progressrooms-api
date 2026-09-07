import uuid
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember
from app.models.schedule import ClassOccurrence
from app.models.commerce import SessionEnrollment
from app.models.attendance import AttendanceRecord
from app.modules.organizations.dependencies import require_org_role
from app.common.enums import UserRole, AttendanceStatus
from app.models.user import User
from app.modules.auth.dependencies import get_current_user

router = APIRouter(prefix="/organizations/{org_id}/occurrences/{occurrence_id}", tags=["Attendance"])

class MarkAttendancePayload(BaseModel):
    enrollment_id: uuid.UUID
    status: AttendanceStatus = AttendanceStatus.ATTENDED
    notes: str | None = None

@router.get("/attendance")
async def get_attendance_roster(
    org_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.INSTRUCTOR])),
    db: AsyncSession = Depends(get_db)
):
    occ_stmt = select(ClassOccurrence).where(ClassOccurrence.id == occurrence_id, ClassOccurrence.organization_id == org_id)
    occ_res = await db.execute(occ_stmt)
    occ = occ_res.scalar_one_or_none()
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class occurrence not found")

    # Get active enrollments for this session
    enr_stmt = select(SessionEnrollment).options(
        selectinload(SessionEnrollment.user),
        selectinload(SessionEnrollment.attendance_records)
    ).where(
        SessionEnrollment.session_id == occ.session_id,
        SessionEnrollment.status == "ACTIVE"
    )
    enr_res = await db.execute(enr_stmt)
    enrollments = enr_res.scalars().all()

    roster = []
    for enr in enrollments:
        att = next((a for a in enr.attendance_records if a.class_occurrence_id == occurrence_id), None)
        roster.append({
            "enrollment_id": str(enr.id),
            "student_id": str(enr.user_id),
            "student_name": enr.user.full_name,
            "student_email": enr.user.email,
            "attendance_status": att.status if att else "UNMARKED",
            "sessions_attended": enr.sessions_attended,
            "total_sessions": enr.total_sessions
        })

    return roster

@router.post("/attendance")
async def mark_attendance(
    org_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    payload: MarkAttendancePayload,
    current_user: User = Depends(get_current_user),
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.INSTRUCTOR])),
    db: AsyncSession = Depends(get_db)
):
    enr_stmt = select(SessionEnrollment).where(SessionEnrollment.id == payload.enrollment_id)
    enr_res = await db.execute(enr_stmt)
    enr = enr_res.scalar_one_or_none()
    if not enr:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enrollment not found")

    att_stmt = select(AttendanceRecord).where(
        AttendanceRecord.class_occurrence_id == occurrence_id,
        AttendanceRecord.session_enrollment_id == enr.id
    )
    att_res = await db.execute(att_stmt)
    att = att_res.scalar_one_or_none()

    if not att:
        att = AttendanceRecord(
            class_occurrence_id=occurrence_id,
            session_enrollment_id=enr.id,
            status=payload.status.value,
            marked_by_user_id=current_user.id,
            notes=payload.notes
        )
        db.add(att)
        if payload.status == AttendanceStatus.ATTENDED:
            enr.sessions_attended += 1
    else:
        old_status = att.status
        att.status = payload.status.value
        att.notes = payload.notes
        if old_status != AttendanceStatus.ATTENDED.value and payload.status == AttendanceStatus.ATTENDED:
            enr.sessions_attended += 1
        elif old_status == AttendanceStatus.ATTENDED.value and payload.status != AttendanceStatus.ATTENDED:
            enr.sessions_attended = max(0, enr.sessions_attended - 1)

    await db.commit()
    return {"message": "Attendance marked successfully", "status": payload.status.value}
