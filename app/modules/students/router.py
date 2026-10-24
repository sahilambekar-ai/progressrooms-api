import math
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from app.common.phone import PhoneNumber
from datetime import datetime, timezone
from typing import Literal
from app.models.session import Session, SessionPricing, SessionInstructor
from sqlalchemy import select, func, or_, asc, desc
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import OrganizationStudent, OrganizationMember
from app.models.user import User
from app.models.commerce import SessionEnrollment
from app.modules.auth.dependencies import get_current_user
from app.modules.organizations.dependencies import resolve_staff_studio
from app.modules.classes.service import auto_complete_expired_one_on_one_classes

router = APIRouter(prefix="/students", tags=["Students Directory"])

class StudentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    full_name: str | None = Field(None, min_length=1, max_length=255)
    phone: PhoneNumber
    whatsapp_number: PhoneNumber | None = None
    address: str | None = Field(None, max_length=1000)
    location: str | None = Field(None, max_length=255)
    status: Literal["ACTIVE", "INACTIVE", "SUSPENDED"] | None = None
    student_notes: str | None = Field(None, max_length=5000)
    teacher_member_id: uuid.UUID | None = None

@router.get("")
async def list_students(page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100),
                        search: str | None = None, status: str | None = None, studio_id: str | None = None,
                        sort_by: Literal["name", "status", "joined"] = "name",
                        sort_order: Literal["asc", "desc"] = "asc",
                        current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    org, member = await resolve_staff_studio(db, current_user, studio_id)
    await auto_complete_expired_one_on_one_classes(db, org.id)
    stmt = select(OrganizationStudent, User).join(User).where(OrganizationStudent.organization_id == org.id)
    if search:

        stmt = stmt.where(or_(User.full_name.ilike(f"%{search}%"), User.email.ilike(f"%{search}%"), User.phone.ilike(f"%{search}%")))
    if status and status != "ALL":
        stmt = stmt.where(OrganizationStudent.status == status)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    sort_column = {"name": User.full_name, "status": OrganizationStudent.status,
                   "joined": OrganizationStudent.created_at}[sort_by]
    ordering = desc(sort_column) if sort_order == "desc" else asc(sort_column)
    rows = (await db.execute(stmt.order_by(ordering, OrganizationStudent.id)
                             .offset((page-1)*page_size).limit(page_size))).all()
    enrollment_totals = {}
    if rows:
        totals = await db.execute(select(
            SessionEnrollment.user_id,
            func.count(SessionEnrollment.id),
            func.sum(SessionEnrollment.sessions_attended),
            func.sum(SessionEnrollment.total_sessions),
        ).where(
            SessionEnrollment.organization_id == org.id,
            SessionEnrollment.user_id.in_([user.id for _, user in rows]),
        ).group_by(SessionEnrollment.user_id))
        enrollment_totals = {user_id: (count, attended or 0, total or 0)
                             for user_id, count, attended, total in totals}
    items = []
    for record, user in rows:
        count, attended, total_sessions = enrollment_totals.get(user.id, (0, 0, 0))
        items.append(dict(id=str(user.id), student_record_id=str(record.id), name=user.full_name, email=user.email,
                          phone=user.phone, whatsapp_number=user.whatsapp_number, address=user.address, location=user.location, student_notes=record.student_notes,
                          teacher_member_id=str(record.teacher_member_id) if record.teacher_member_id else None,
                          status=record.status, is_verified=user.is_verified, account_status=user.account_status,
                          plan="Enrolled" if count else "No enrollment",
                          attended=attended, total=total_sessions))
    return dict(items=items, total_items=total, page=page, page_size=page_size,
                total_pages=max(1, math.ceil(total/page_size)), sort_by=sort_by, sort_order=sort_order)

@router.patch("/{record_id}")
async def update_student(record_id: uuid.UUID, payload: StudentUpdate,
                         current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    record = await db.get(OrganizationStudent, record_id)
    if not record:
        raise HTTPException(404, "Student not found")
    org, member = await resolve_staff_studio(db, current_user, record.organization_id)
    student = await db.get(User, record.user_id)
    if not student or student.role != "STUDENT" or student.deleted_at:
        raise HTTPException(404, "Student not found")
    if "full_name" in payload.model_fields_set and payload.full_name is None:
        raise HTTPException(422, "Student name is required")
    for key in ("full_name", "phone", "whatsapp_number", "address", "location"):
        if key in payload.model_fields_set:
            setattr(student, key, getattr(payload, key))
    if "status" in payload.model_fields_set:
        record.status = payload.status
    if "teacher_member_id" in payload.model_fields_set:
        if current_user.role != "STUDIO":
            raise HTTPException(403, "Only the studio owner can assign teachers")
        if payload.teacher_member_id:
            teacher = await db.scalar(select(OrganizationMember).where(
                OrganizationMember.id == payload.teacher_member_id, OrganizationMember.organization_id == org.id,
                OrganizationMember.can_teach.is_(True), OrganizationMember.is_active == True))
            if not teacher:
                raise HTTPException(422, "Choose an active teacher from this studio")
        record.teacher_member_id = payload.teacher_member_id
    if "student_notes" in payload.model_fields_set:
        record.student_notes = payload.student_notes
    await db.commit()
    return {"message": "Student updated"}

from pydantic import EmailStr

class AddStudent(BaseModel):
    email: EmailStr
    class_id: uuid.UUID
    teacher_member_id: uuid.UUID | None = None

@router.post("", status_code=201)
async def add_registered_student(payload: AddStudent, current_user: User = Depends(get_current_user),
                                  db: AsyncSession = Depends(get_db)):
    """Owner-managed class admission; does not create an account or record a payment."""
    from datetime import datetime, timezone
    from app.models.session import Session, SessionInstructor
    org, _ = await resolve_staff_studio(db, current_user, owner_only=True)
    student = await db.scalar(select(User).where(User.email == payload.email.lower(), User.role == "STUDENT",
        User.is_active == True, User.is_verified == True, User.deleted_at.is_(None), User.account_status == "ACTIVE"))
    if not student:
        raise HTTPException(422, "The student must register and verify their account first")
    if not student.phone:
        raise HTTPException(422, "Add a phone number to the student profile before enrolling")
    cls = await db.scalar(select(Session).where(Session.id == payload.class_id, Session.organization_id == org.id,
        Session.deleted_at.is_(None), Session.is_active == True).with_for_update())
    if not cls:
        raise HTTPException(422, "Choose a class in your studio")
    teacher = None
    if payload.teacher_member_id:
        teacher = await db.scalar(select(OrganizationMember).where(OrganizationMember.id == payload.teacher_member_id,
            OrganizationMember.organization_id == org.id, OrganizationMember.can_teach.is_(True), OrganizationMember.is_active == True))
        if not teacher:
            raise HTTPException(422, "Choose an active teacher from this studio")
    record = await db.scalar(select(OrganizationStudent).where(OrganizationStudent.organization_id == org.id,
        OrganizationStudent.user_id == student.id))
    if not record:
        record = OrganizationStudent(organization_id=org.id, user_id=student.id, teacher_member_id=teacher.id if teacher else None)
        db.add(record)
    elif teacher:
        record.teacher_member_id = teacher.id
    existing = await db.scalar(select(SessionEnrollment.id).where(SessionEnrollment.session_id == cls.id,
        SessionEnrollment.user_id == student.id, SessionEnrollment.status.in_(["ACTIVE", "PAUSED"])))
    if not existing:
        count = await db.scalar(select(func.count(SessionEnrollment.id)).where(
            SessionEnrollment.session_id == cls.id, SessionEnrollment.status == "ACTIVE")) or 0
        if cls.capacity is not None and count >= cls.capacity:
            raise HTTPException(409, "Class is full")
        pricing = await db.scalar(select(SessionPricing).where(SessionPricing.session_id == cls.id))
        db.add(SessionEnrollment(organization_id=org.id, session_id=cls.id, user_id=student.id,
            status="ACTIVE", valid_from=datetime.now(timezone.utc), sessions_attended=0,
            billing_value=pricing.price if pricing else 0,
            billing_currency=pricing.currency if pricing else org.currency,
            billing_type=pricing.pricing_type if pricing else "MANUAL",
            billing_label=cls.name))
    await db.commit()
    return {"message": "Student added to the class", "student_record_id": str(record.id)}


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def enrollment_details(enrollment, name):
    until = utc(enrollment.valid_until)
    reference = utc(enrollment.paused_at) or datetime.now(timezone.utc)
    remaining = max(0, (until - reference).total_seconds()) if until else None
    return dict(id=str(enrollment.id), name=name, status=enrollment.status,
                valid_until=until.isoformat() if until else None,
                paused_at=enrollment.paused_at.isoformat() if enrollment.paused_at else None,
                remaining_days=round(remaining / 86400, 2) if remaining is not None else None,
                total_sessions=enrollment.total_sessions, sessions_attended=enrollment.sessions_attended)


@router.get("/{record_id}/enrollments")
async def student_enrollments(record_id: uuid.UUID, current_user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    record = await db.get(OrganizationStudent, record_id)
    if not record:
        raise HTTPException(404, "Student not found")
    await resolve_staff_studio(db, current_user, record.organization_id)
    rows = (await db.execute(select(SessionEnrollment, Session.name).join(Session).where(
        SessionEnrollment.organization_id == record.organization_id,
        SessionEnrollment.user_id == record.user_id).order_by(SessionEnrollment.created_at.desc()))).all()
    return [enrollment_details(enrollment, name) for enrollment, name in rows]


@router.post("/{record_id}/enrollments/{enrollment_id}/{action}")
async def change_enrollment_pause(record_id: uuid.UUID, enrollment_id: uuid.UUID,
                                  action: Literal["pause", "resume", "cancel"],
                                  current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    record = await db.get(OrganizationStudent, record_id)
    if not record:
        raise HTTPException(404, "Student not found")
    await resolve_staff_studio(db, current_user, record.organization_id)
    enrollment = await db.scalar(select(SessionEnrollment).where(
        SessionEnrollment.id == enrollment_id, SessionEnrollment.organization_id == record.organization_id,
        SessionEnrollment.user_id == record.user_id).with_for_update())
    if not enrollment:
        raise HTTPException(404, "Enrollment not found")
    now = datetime.now(timezone.utc)
    if action == "cancel":
        if enrollment.status not in {"ACTIVE", "PAUSED"}:
            raise HTTPException(409, "Only an active or paused pass can be cancelled")
        enrollment.status = "CANCELLED"
        enrollment.paused_at = None
        has_remaining_pass = await db.scalar(select(SessionEnrollment.id).where(
            SessionEnrollment.organization_id == record.organization_id,
            SessionEnrollment.user_id == record.user_id,
            SessionEnrollment.id != enrollment.id,
            SessionEnrollment.status.in_(["ACTIVE", "PAUSED"])))
        if not has_remaining_pass:
            record.status = "INACTIVE"
    elif action == "pause":
        if enrollment.status != "ACTIVE" or enrollment.paused_at:
            raise HTTPException(409, "Only an active pass can be paused")
        if utc(enrollment.valid_from) > now or (enrollment.valid_until and utc(enrollment.valid_until) <= now):
            raise HTTPException(409, "Only a currently valid pass can be paused")
        if enrollment.total_sessions is not None and enrollment.sessions_attended >= enrollment.total_sessions:
            raise HTTPException(409, "This pass has no sessions remaining")
        enrollment.paused_at = now
        enrollment.status = "PAUSED"
    else:
        if enrollment.status != "PAUSED" or not enrollment.paused_at:
            raise HTTPException(409, "Only a paused pass can be resumed")
        if enrollment.valid_until:
            enrollment.valid_until = utc(enrollment.valid_until) + (now - utc(enrollment.paused_at))
        enrollment.paused_at = None
        enrollment.status = "ACTIVE"
    await db.commit()
    return {"message": "Pass cancelled. No payment is returned." if action == "cancel" else "Pass paused" if action == "pause" else "Pass resumed"}
