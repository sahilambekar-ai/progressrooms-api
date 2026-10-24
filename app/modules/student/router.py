from app.modules.attendance.status import calendar_status
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, or_, func, and_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.commerce import SessionEnrollment
from app.models.schedule import ClassOccurrence
from app.models.session import Session
from app.models.organization import Organization, OrganizationMember, OrganizationStudent
from app.models.user import User
from app.models.attendance import AttendanceRecord
from app.modules.auth.dependencies import get_current_user
from app.common.enums import EnrollmentStatus
from app.modules.organizations.zoom_service import get_or_create_occurrence_meeting, ZoomAuthError

from pydantic import BaseModel
from app.models.membership_tier import MembershipTier

class RescheduleBookingRequest(BaseModel):
    target_occurrence_id: uuid.UUID

router = APIRouter(prefix="/student", tags=["Student Portal"])

@router.get("/studios")
async def get_my_studios(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Studios linked by staff assignment or enrollment, including solo teachers."""
    assigned = select(OrganizationStudent.organization_id).where(
        OrganizationStudent.user_id == current_user.id, OrganizationStudent.status == "ACTIVE")
    enrolled = select(SessionEnrollment.organization_id).where(SessionEnrollment.user_id == current_user.id)
    studios = (await db.execute(select(Organization).where(
        Organization.status == "ACTIVE", Organization.deleted_at.is_(None),
        or_(Organization.id.in_(assigned), Organization.id.in_(enrolled))
    ).order_by(Organization.name))).scalars().all()
    return [{"id": str(org.id), "name": org.name, "slug": org.slug} for org in studios]


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

    purchase_ids = {e.purchase_order_id for e in enrollments if e.purchase_order_id}
    usage = {}
    if purchase_ids:
        usage = dict((await db.execute(select(SessionEnrollment.purchase_order_id, func.sum(SessionEnrollment.sessions_attended))
            .where(SessionEnrollment.purchase_order_id.in_(purchase_ids)).group_by(SessionEnrollment.purchase_order_id))).all())

    from app.models.commerce import Order
    purchase_limits = dict((await db.execute(select(Order.id, Order.credit_limit).where(Order.id.in_(purchase_ids)))).all()) if purchase_ids else {}
    enrollment_ids = [e.id for e in enrollments]
    restored_map = {}
    if enrollment_ids:
        restored_rows = (await db.execute(
            select(AttendanceRecord.session_enrollment_id, func.count(AttendanceRecord.id))
            .where(
                AttendanceRecord.session_enrollment_id.in_(enrollment_ids),
                AttendanceRecord.status.in_(["EXCUSED", "CANCELLED"])
            )
            .group_by(AttendanceRecord.session_enrollment_id)
        )).all()
        restored_map = dict(restored_rows)

    tiers = (await db.execute(
        select(MembershipTier.id, MembershipTier.name, MembershipTier.organization_id, MembershipTier.description)
        .where(MembershipTier.is_active == True, MembershipTier.deleted_at.is_(None))
    )).all()
    tier_descriptions = {(t.organization_id, t.name): t.description for t in tiers}
    tier_name_map = {(t.organization_id, t.name): str(t.id) for t in tiers}

    return [
        {
            "purchase_id": str(e.purchase_order_id) if e.purchase_order_id else None,
            "purchase_name": e.billing_label,
            "amount_paid": float(e.billing_value) if e.billing_value is not None else None,
            "currency": e.billing_currency,
            "id": str(e.id),
            "session_id": str(e.session_id),
            "tier_id": tier_name_map.get((e.organization_id, e.billing_label)),
            "session_name": e.session.name if e.session else "Session",
            "description": tier_descriptions.get((e.organization_id, e.billing_label)) or (e.session.full_description if e.session else None),
            "organization_name": e.session.organization.name if e.session and e.session.organization else "Studio",
            "organization_slug": e.session.organization.slug if e.session and e.session.organization else "",
            "status": "EXPIRED" if e.status == "ACTIVE" and e.valid_until and e.valid_until.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc) else e.status,
            "valid_from": e.valid_from.isoformat(),
            "valid_until": e.valid_until.isoformat() if e.valid_until else None,
            "total_sessions": purchase_limits.get(e.purchase_order_id, e.total_sessions),
            "sessions_attended": usage.get(e.purchase_order_id, e.sessions_attended),
            "sessions_restored": restored_map.get(e.id, 0),
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
        SessionEnrollment.status == EnrollmentStatus.ACTIVE.value,
        SessionEnrollment.valid_from <= now,
        or_(SessionEnrollment.valid_until.is_(None), SessionEnrollment.valid_until > now)
    )
    excused_occ_ids = select(AttendanceRecord.class_occurrence_id).join(
        SessionEnrollment, AttendanceRecord.session_enrollment_id == SessionEnrollment.id
    ).where(
        SessionEnrollment.user_id == current_user.id,
        AttendanceRecord.status.in_(["EXCUSED", "CANCELLED"])
    )
    occ_stmt = select(ClassOccurrence).options(
        selectinload(ClassOccurrence.session).selectinload(Session.organization),
        selectinload(ClassOccurrence.instructor).selectinload(OrganizationMember.user)
    ).where(
        ClassOccurrence.session_id.in_(enr_stmt),
        ClassOccurrence.id.not_in(excused_occ_ids),
        ClassOccurrence.status == "SCHEDULED",
        ClassOccurrence.session_id.in_(select(Session.id).join(Organization).where(
            Session.is_active == True, Session.deleted_at.is_(None),
            Organization.status == "ACTIVE", Organization.deleted_at.is_(None))),
        ClassOccurrence.actual_end_at > now,
        ClassOccurrence.actual_start_at <= future
    ).order_by(ClassOccurrence.actual_start_at.asc())

    occ_res = await db.execute(occ_stmt)
    occurrences = occ_res.scalars().all()

    results = []
    for o in occurrences:
        occ_start = o.actual_start_at.replace(tzinfo=timezone.utc) if o.actual_start_at.tzinfo is None else o.actual_start_at
        occ_end = o.actual_end_at.replace(tzinfo=timezone.utc) if o.actual_end_at.tzinfo is None else o.actual_end_at
        seconds_to_start = (occ_start - now).total_seconds()

        if o.status not in {"SCHEDULED", "RESCHEDULED", "IN_PROGRESS"}:
            can_join = False
            reason = f"Class is {o.status.lower()}"
        elif occ_end <= now:
            can_join = False
            reason = "Class has ended"
        elif seconds_to_start > 900:
            can_join = False
            mins = int(seconds_to_start // 60)
            reason = "Opens 15 minutes before class" if mins > 15 else f"Opens in {mins} minutes"
        else:
            can_join = True
            reason = "Ready to join"

        results.append({
            "id": str(o.id),
            "name": o.session.name if o.session else "Session",
            "session_name": o.session.name if o.session else "Session",
            "description": o.session.full_description if o.session else None,
            "organization_name": o.session.organization.name if o.session and o.session.organization else "Studio",
            "organization_slug": o.session.organization.slug if o.session and o.session.organization else "",
            "instructor_name": o.instructor.user.full_name if o.instructor and o.instructor.user else "Teacher",
            "start_at": occ_start.isoformat(),
            "end_at": occ_end.isoformat(),
            "can_join": can_join,
            "eligibility_reason": reason
        })

    return results

@router.get("/classes/{occurrence_id}/meeting-url")
async def get_class_meeting_url(
    occurrence_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    occ_stmt = select(ClassOccurrence).options(
        selectinload(ClassOccurrence.session).selectinload(Session.organization)
    ).where(ClassOccurrence.id == occurrence_id)
    occ_res = await db.execute(occ_stmt)
    occ = occ_res.scalar_one_or_none()
    if not occ or not occ.session or not occ.session.is_active or occ.session.deleted_at:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class occurrence not found")

    # Check enrollment & available credits
    now = datetime.now(timezone.utc)
    enr_stmt = select(SessionEnrollment).where(
        SessionEnrollment.session_id == occ.session_id,
        SessionEnrollment.user_id == current_user.id,
        SessionEnrollment.status == EnrollmentStatus.ACTIVE.value,
        SessionEnrollment.valid_from <= now,
        or_(SessionEnrollment.valid_until.is_(None), SessionEnrollment.valid_until > now)
    )
    from app.modules.commerce.service import available_credit
    candidates = (await db.execute(enr_stmt)).scalars().all()
    enr = None
    for candidate in candidates:
        if await available_credit(db, candidate, lock_pass=False):
            enr = candidate
            break
    if not enr:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have an active enrollment or available credits for this class."
        )

    # Check join time window: within 15 minutes of start and before end
    from app.modules.billing.saas import require_operational_access
    await require_operational_access(db, occ.organization_id)
    occ_start = occ.actual_start_at.replace(tzinfo=timezone.utc) if occ.actual_start_at.tzinfo is None else occ.actual_start_at
    occ_end = occ.actual_end_at.replace(tzinfo=timezone.utc) if occ.actual_end_at.tzinfo is None else occ.actual_end_at
    seconds_until_start = (occ_start - now).total_seconds()

    if occ_end <= now or occ.status not in {"SCHEDULED", "RESCHEDULED", "IN_PROGRESS"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This class is no longer open for joining."
        )
    if seconds_until_start > 900: # more than 15 mins before start
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Meeting link unlocks 15 minutes before scheduled class start time."
        )

    # Provision or retrieve real meeting room
    organization = occ.session.organization
    try:
        meeting_data = await get_or_create_occurrence_meeting(db, occ, organization, is_host=False)
        from app.modules.billing.platform import record_join
        await record_join(db, organization, current_user, enr, occ)
        await db.commit()
        return {
            "meeting_url": meeting_data["target_url"],
            "provider": meeting_data["provider"],
            "is_simulated": meeting_data.get("is_simulated", False)
        }
    except ZoomAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

@router.post("/classes/{occurrence_id}/cancel")
async def cancel_student_class_booking(
    occurrence_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    occ = await db.scalar(
        select(ClassOccurrence).options(
            selectinload(ClassOccurrence.session).selectinload(Session.organization)
        ).where(ClassOccurrence.id == occurrence_id)
    )
    if not occ:
        raise HTTPException(status_code=404, detail="Class occurrence not found")

    now = datetime.now(timezone.utc)
    occ_start = occ.actual_start_at.replace(tzinfo=timezone.utc) if occ.actual_start_at.tzinfo is None else occ.actual_start_at

    # Check 2-hour cancellation policy
    if occ_start - now < timedelta(hours=2):
        raise HTTPException(
            status_code=400,
            detail="Late cancellation: bookings can only be cancelled at least 2 hours before start time. This booking cannot be cancelled in the app. Contact your teacher or studio for help. No payment is returned."
        )

    # Find the active enrollment for this class
    enr = await db.scalar(
        select(SessionEnrollment).where(
            SessionEnrollment.session_id == occ.session_id,
            SessionEnrollment.user_id == current_user.id,
            SessionEnrollment.status == "ACTIVE"
        )
    )
    if not enr:
        raise HTTPException(status_code=403, detail="No active enrollment found for this class.")

    # Find attendance record if any
    att = await db.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.class_occurrence_id == occ.id,
            AttendanceRecord.session_enrollment_id == enr.id
        )
    )
    if att:
        if att.status == "ATTENDED" and enr.sessions_attended > 0:
            enr.sessions_attended -= 1
        att.status = "EXCUSED"
        att.notes = "Cancelled by student >2 hours prior to start"
    else:
        att = AttendanceRecord(
            class_occurrence_id=occ.id,
            session_enrollment_id=enr.id,
            status="EXCUSED",
            notes="Cancelled by student >2 hours prior to start"
        )
        db.add(att)

    await db.commit()
    return {
        "message": "Class booking cancelled successfully. Your session credit remains available on your pass. No payment is returned.",
        "credit_preserved": True,
        "remaining_credits": max(0, (enr.total_sessions or 0) - enr.sessions_attended) if enr.total_sessions else None
    }

@router.get("/classes/{occurrence_id}/available-reschedule-slots")
async def get_available_reschedule_slots(
    occurrence_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    occ = await db.scalar(
        select(ClassOccurrence).options(
            selectinload(ClassOccurrence.session).selectinload(Session.organization),
            selectinload(ClassOccurrence.instructor).selectinload(OrganizationMember.user)
        ).where(ClassOccurrence.id == occurrence_id)
    )
    if not occ:
        raise HTTPException(status_code=404, detail="Class occurrence not found")

    now = datetime.now(timezone.utc)
    occ_start = occ.actual_start_at.replace(tzinfo=timezone.utc) if occ.actual_start_at.tzinfo is None else occ.actual_start_at

    if occ_start - now < timedelta(hours=2):
        raise HTTPException(
            status_code=400,
            detail="Rescheduling is only allowed at least 2 hours before scheduled class start time."
        )

    enr = await db.scalar(
        select(SessionEnrollment).where(
            SessionEnrollment.session_id == occ.session_id,
            SessionEnrollment.user_id == current_user.id,
            SessionEnrollment.status == "ACTIVE"
        )
    )
    if not enr:
        raise HTTPException(status_code=403, detail="No active enrollment found for this session.")

    min_reschedule_time = now + timedelta(hours=2)
    max_reschedule_time = now + timedelta(days=30)

    slots_stmt = select(ClassOccurrence).options(
        selectinload(ClassOccurrence.instructor).selectinload(OrganizationMember.user)
    ).where(
        ClassOccurrence.session_id == occ.session_id,
        ClassOccurrence.id != occ.id,
        ClassOccurrence.status == "SCHEDULED",
        ClassOccurrence.actual_start_at >= min_reschedule_time,
        ClassOccurrence.actual_start_at <= max_reschedule_time
    ).order_by(ClassOccurrence.actual_start_at.asc())

    slots = (await db.execute(slots_stmt)).scalars().all()

    results = []
    for s in slots:
        start_dt = s.actual_start_at.replace(tzinfo=timezone.utc) if s.actual_start_at.tzinfo is None else s.actual_start_at
        end_dt = s.actual_end_at.replace(tzinfo=timezone.utc) if s.actual_end_at.tzinfo is None else s.actual_end_at

        # Exclude slots where already attending
        existing_att = await db.scalar(
            select(AttendanceRecord).where(
                AttendanceRecord.class_occurrence_id == s.id,
                AttendanceRecord.session_enrollment_id == enr.id
            )
        )
        if existing_att and existing_att.status == "ATTENDED":
            continue

        results.append({
            "id": str(s.id),
            "start_at": start_dt.isoformat(),
            "end_at": end_dt.isoformat(),
            "instructor_name": s.instructor.user.full_name if s.instructor and s.instructor.user else "Teacher",
            "session_name": occ.session.name if occ.session else "Session"
        })
    return results

@router.post("/classes/{occurrence_id}/reschedule")
async def reschedule_student_class_booking(
    occurrence_id: uuid.UUID,
    payload: RescheduleBookingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    source_occ = await db.scalar(
        select(ClassOccurrence).options(
            selectinload(ClassOccurrence.session)
        ).where(ClassOccurrence.id == occurrence_id)
    )
    if not source_occ:
        raise HTTPException(status_code=404, detail="Original class occurrence not found")

    target_occ = await db.scalar(
        select(ClassOccurrence).options(
            selectinload(ClassOccurrence.session)
        ).where(ClassOccurrence.id == payload.target_occurrence_id)
    )
    if not target_occ:
        raise HTTPException(status_code=404, detail="Target class occurrence not found")

    if target_occ.session_id != source_occ.session_id:
        raise HTTPException(status_code=400, detail="Target class must belong to the same session offering.")

    now = datetime.now(timezone.utc)
    source_start = source_occ.actual_start_at.replace(tzinfo=timezone.utc) if source_occ.actual_start_at.tzinfo is None else source_occ.actual_start_at
    target_start = target_occ.actual_start_at.replace(tzinfo=timezone.utc) if target_occ.actual_start_at.tzinfo is None else target_occ.actual_start_at

    if source_start - now < timedelta(hours=2):
        raise HTTPException(
            status_code=400,
            detail="Rescheduling is only allowed at least 2 hours before scheduled class start time."
        )

    if target_start - now < timedelta(hours=2):
        raise HTTPException(
            status_code=400,
            detail="Target class must start at least 2 hours from now."
        )

    if target_occ.status not in {"SCHEDULED", "RESCHEDULED", "IN_PROGRESS"}:
        raise HTTPException(status_code=400, detail="Target class is not open for booking.")

    enr = await db.scalar(
        select(SessionEnrollment).where(
            SessionEnrollment.session_id == source_occ.session_id,
            SessionEnrollment.user_id == current_user.id,
            SessionEnrollment.status == "ACTIVE"
        )
    )
    if not enr:
        raise HTTPException(status_code=403, detail="No active enrollment found for this session.")

    # Excuse source occurrence so it doesn't appear in upcoming classes and frees up slot
    source_att = await db.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.class_occurrence_id == source_occ.id,
            AttendanceRecord.session_enrollment_id == enr.id
        )
    )
    if source_att:
        if source_att.status == "ATTENDED" and enr.sessions_attended > 0:
            enr.sessions_attended -= 1
        source_att.status = "EXCUSED"
        source_att.notes = f"Rescheduled by student to {target_occ.id}"
    else:
        source_att = AttendanceRecord(
            class_occurrence_id=source_occ.id,
            session_enrollment_id=enr.id,
            status="EXCUSED",
            notes=f"Rescheduled by student to {target_occ.id}"
        )
        db.add(source_att)

    # If target occurrence was previously marked EXCUSED or CANCELLED, remove it
    target_att = await db.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.class_occurrence_id == target_occ.id,
            AttendanceRecord.session_enrollment_id == enr.id
        )
    )
    if target_att and target_att.status in ("EXCUSED", "CANCELLED"):
        await db.delete(target_att)

    await db.commit()
    return {
        "message": "Class rescheduled successfully.",
        "target_occurrence_id": str(target_occ.id),
        "target_start_at": target_start.isoformat(),
        "remaining_credits": max(0, (enr.total_sessions or 0) - enr.sessions_attended) if enr.total_sessions else None
    }

@router.get("/attendance")
async def get_my_attendance(
    month: str = Query(pattern=r"^\d{4}-\d{2}$"),
    studio_id: uuid.UUID | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    try:
        year, number = map(int, month.split("-"))
        if not 1 <= number <= 12 or not 1900 <= year <= 2200:
            raise ValueError()
    except Exception:
        raise HTTPException(422, "Choose a valid calendar month")

    default_zone = ZoneInfo("Asia/Kolkata")
    start = datetime(year, number, 1, tzinfo=default_zone).astimezone(timezone.utc)
    end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=default_zone).astimezone(timezone.utc)

    stmt = (
        select(ClassOccurrence, Session, AttendanceRecord, SessionEnrollment, Organization)
        .options(selectinload(ClassOccurrence.instructor).selectinload(OrganizationMember.user))
        .join(Session, Session.id == ClassOccurrence.session_id)
        .join(Organization, Organization.id == Session.organization_id)
        .join(
            SessionEnrollment,
            and_(
                SessionEnrollment.session_id == Session.id,
                SessionEnrollment.user_id == current_user.id
            )
        )
        .outerjoin(
            AttendanceRecord,
            and_(
                AttendanceRecord.class_occurrence_id == ClassOccurrence.id,
                AttendanceRecord.session_enrollment_id == SessionEnrollment.id
            )
        )
        .where(
            ClassOccurrence.actual_start_at >= start,
            ClassOccurrence.actual_start_at < end,
            ClassOccurrence.actual_start_at >= SessionEnrollment.valid_from,
            (SessionEnrollment.valid_until.is_(None) | (ClassOccurrence.actual_start_at <= SessionEnrollment.valid_until))
        )
        .order_by(ClassOccurrence.actual_start_at)
    )

    if studio_id:
        stmt = stmt.where(Organization.id == studio_id)

    rows = (await db.execute(stmt)).all()
    now_utc = datetime.now(timezone.utc)
    result = []

    for occ, session, record, enrollment, org in rows:
        org_zone = ZoneInfo(org.timezone or "Asia/Kolkata")
        occ_start = occ.actual_start_at.replace(tzinfo=timezone.utc) if occ.actual_start_at.tzinfo is None else occ.actual_start_at
        local = occ_start.astimezone(org_zone)
        status_val = calendar_status(occ.actual_start_at, occ.status, record.status if record else None, now_utc)

        teacher_name = (
            occ.instructor.user.full_name
            if occ.instructor and occ.instructor.user
            else None
        )

        event = {
            "id": f"{occ.id}:{enrollment.id}",
            "occurrenceId": str(occ.id),
            "enrollmentId": str(enrollment.id),
            "date": local.date().isoformat(),
            "time": local.strftime("%I:%M %p"),
            "startAt": occ_start.isoformat(),
            "sessionTitle": session.name,
            "status": status_val,
            "remarks": record.notes if record else "",
            "studentName": current_user.full_name,
            "organizationId": str(org.id),
            "organizationName": org.name,
            "organizationSlug": org.slug,
            "teacherName": teacher_name
        }
        result.append(event)
    return result

