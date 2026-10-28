from app.modules.attendance.status import calendar_status
import uuid
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember
from app.models.user import User
from app.models.schedule import ClassOccurrence
from app.models.session import Session
from app.models.commerce import SessionEnrollment
from app.models.attendance import AttendanceRecord, TeacherAttendance
from app.modules.organizations.dependencies import require_org_role
from app.modules.auth.dependencies import get_current_user
from app.modules.scheduling.access import assigned_occurrence
from app.common.enums import UserRole

router = APIRouter(prefix="/organizations/{org_id}/attendance", tags=["Attendance calendar"])
read_access = require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.INSTRUCTOR])
write_access = require_org_role([UserRole.OWNER, UserRole.ADMIN])


def month_bounds(month, org):
    year, number = map(int, month.split("-"))
    if not 1 <= number <= 12 or not 1900 <= year <= 2200:
        raise HTTPException(422, "Choose a valid calendar month")
    zone = ZoneInfo(org.timezone or "Asia/Kolkata")
    start = datetime(year, number, 1, tzinfo=zone)
    end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), zone


def event_dict(occ, session, status, notes, zone, **extra):
    local = occ.actual_start_at.replace(tzinfo=timezone.utc) if occ.actual_start_at.tzinfo is None else occ.actual_start_at
    local = local.astimezone(zone)
    final_status = calendar_status(occ.actual_start_at, occ.status, status)
    return dict(id=str(occ.id), occurrenceId=str(occ.id), date=local.date().isoformat(),
                time=local.strftime("%I:%M %p"), startAt=local.isoformat(),
                endAt=(occ.actual_end_at.replace(tzinfo=timezone.utc) if occ.actual_end_at.tzinfo is None else occ.actual_end_at).isoformat(),
                meetingAvailable=occ.status != "CANCELLED",
                meetingUrl=occ.meeting_url if occ.status != "CANCELLED" else None, sessionTitle=session.name, description=session.full_description,
                status=final_status,
                remarks=notes or "", **extra)


@router.get("/teachers/{teacher_id}")
async def teacher_calendar(org_id: uuid.UUID, teacher_id: uuid.UUID,
        month: str = Query(pattern=r"^\d{4}-\d{2}$"), tenant=Depends(read_access), db: AsyncSession = Depends(get_db)):
    org, member = tenant
    if member and member.role == "TEACHER" and member.id != teacher_id:
        raise HTTPException(403, "You can only review your own teacher attendance")
    teacher = await db.scalar(select(OrganizationMember).where(OrganizationMember.id == teacher_id,
        OrganizationMember.organization_id == org_id, OrganizationMember.can_teach.is_(True)))
    if not teacher:
        raise HTTPException(404, "Teacher not found")
    start, end, zone = month_bounds(month, org)
    rows = (await db.execute(select(ClassOccurrence, Session, TeacherAttendance)
        .join(Session, Session.id == ClassOccurrence.session_id)
        .outerjoin(TeacherAttendance, and_(TeacherAttendance.class_occurrence_id == ClassOccurrence.id,
                                          TeacherAttendance.teacher_member_id == teacher_id))
        .where(ClassOccurrence.organization_id == org_id, assigned_occurrence(teacher_id),
               ClassOccurrence.actual_start_at >= start, ClassOccurrence.actual_start_at < end)
        .order_by(ClassOccurrence.actual_start_at))).all()
    name = await db.scalar(select(User.full_name).where(User.id == teacher.user_id))
    return [event_dict(occ, session, record.status if record else None, record.notes if record else None,
                       zone, teacherName=name) for occ, session, record in rows]


class TeacherAttendanceUpdate(BaseModel):
    status: Literal["PRESENT", "ABSENT", "EXCUSED", "CANCELLED"]
    notes: str | None = Field(default=None, max_length=2000)


@router.put("/teachers/{teacher_id}/occurrences/{occurrence_id}")
async def save_teacher_attendance(org_id: uuid.UUID, teacher_id: uuid.UUID, occurrence_id: uuid.UUID,
        payload: TeacherAttendanceUpdate, tenant=Depends(write_access),
        user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    teacher = await db.scalar(select(OrganizationMember).where(OrganizationMember.id == teacher_id,
        OrganizationMember.organization_id == org_id, OrganizationMember.can_teach.is_(True)))
    if not teacher:
        raise HTTPException(404, "Teacher not found")
    # Lock the occurrence so concurrent saves cannot create duplicate attendance rows.
    occurrence = await db.scalar(select(ClassOccurrence).where(ClassOccurrence.id == occurrence_id,
        ClassOccurrence.organization_id == org_id, assigned_occurrence(teacher_id)).with_for_update())
    if not occurrence:
        raise HTTPException(404, "Class is not assigned to this teacher")
    record = await db.scalar(select(TeacherAttendance).where(TeacherAttendance.class_occurrence_id == occurrence_id,
        TeacherAttendance.teacher_member_id == teacher_id))
    if record is None:
        record = TeacherAttendance(class_occurrence_id=occurrence_id, teacher_member_id=teacher_id)
        db.add(record)
    record.status, record.notes, record.marked_by_user_id = ("CANCELLED" if payload.status == "EXCUSED" else payload.status), payload.notes, user.id
    await db.commit()
    return {"status": "EXCUSED" if record.status == "CANCELLED" else record.status, "notes": record.notes}


@router.get("/students/{student_id}")
async def student_calendar(org_id: uuid.UUID, student_id: uuid.UUID,
        month: str = Query(pattern=r"^\d{4}-\d{2}$"), tenant=Depends(read_access), db: AsyncSession = Depends(get_db)):
    org, member = tenant
    start, end, zone = month_bounds(month, org)
    stmt = select(ClassOccurrence, Session, AttendanceRecord, SessionEnrollment).join(Session,
        Session.id == ClassOccurrence.session_id).join(SessionEnrollment, and_(
        SessionEnrollment.session_id == Session.id, SessionEnrollment.user_id == student_id,
        SessionEnrollment.organization_id == org_id)).outerjoin(AttendanceRecord, and_(
        AttendanceRecord.class_occurrence_id == ClassOccurrence.id,
        AttendanceRecord.session_enrollment_id == SessionEnrollment.id)).where(
        ClassOccurrence.organization_id == org_id, ClassOccurrence.actual_start_at >= start,
        ClassOccurrence.actual_start_at < end,
        # Historical records remain visible even when a pass later expires.
        ClassOccurrence.actual_start_at >= SessionEnrollment.valid_from,
        (SessionEnrollment.valid_until.is_(None) | (ClassOccurrence.actual_start_at <= SessionEnrollment.valid_until)))
    if member and member.role == "TEACHER":
        stmt = stmt.where(assigned_occurrence(member.id))
    rows = (await db.execute(stmt.order_by(ClassOccurrence.actual_start_at))).all()
    result = []
    for occ, session, record, enrollment in rows:
        status = {"ATTENDED": "PRESENT", "ABSENT": "ABSENT", "EXCUSED": "EXCUSED"}.get(record.status) if record else None
        event = event_dict(occ, session, status, record.notes if record else None, zone)
        event["id"] = f"{occ.id}:{enrollment.id}"
        event["enrollmentId"] = str(enrollment.id)
        result.append(event)
    return result
