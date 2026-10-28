import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import secrets
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update, delete
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember, OrganizationStudent
from app.models.user import User
from app.models.session import ClassType, Session, SessionInstructor, SessionPricing, SessionRequirement
from app.models.schedule import SessionScheduleRule, ClassOccurrence
from app.models.commerce import SessionEnrollment
from app.models.attendance import AttendanceRecord
from app.modules.organizations.dependencies import get_current_tenant_member, require_org_role
from app.modules.classes.schemas import (
    ClassTypeCreate,
    SessionCreate,
    SessionUpdate,
    SessionToggleStatus,
    OneOnOneSessionCreate,
)
from app.common.enums import UserRole, SessionType, SkillLevel, PricingType, InstructorRole, OccurrenceStatus, AttendanceStatus
from app.modules.scheduling.engine import SchedulingEngine
from app.modules.classes.service import auto_complete_expired_one_on_one_classes

router = APIRouter(prefix="/organizations/{org_id}", tags=["Classes & Sessions"])


@router.delete("/classes/{class_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/sessions/{class_id}", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
async def delete_session(
    org_id: uuid.UUID,
    class_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db),
):
    from app.modules.zoom.service import lock_studio
    from app.modules.zoom.meetings import synchronize
    await lock_studio(db, org_id)
    session = await db.scalar(select(Session).where(
        Session.id == class_id, Session.organization_id == org_id, Session.deleted_at.is_(None)))
    if not session:
        raise HTTPException(404, "Class not found")

    now = datetime.now(timezone.utc)
    future = (await db.scalars(select(ClassOccurrence).where(ClassOccurrence.session_id == class_id,
        ClassOccurrence.organization_id == org_id, ClassOccurrence.status.in_(["SCHEDULED", "RESCHEDULED", "IN_PROGRESS"]),
        ClassOccurrence.actual_end_at > now).with_for_update())).all()
    # Remote deletes are idempotent; a provider outage keeps the local batch intact for retry.
    for occurrence in future:
        await synchronize(db, occurrence, "cancel")
    session.deleted_at = now
    session.is_active = False
    await db.execute(update(SessionScheduleRule).where(
        SessionScheduleRule.session_id == class_id).values(is_active=False))
    for occurrence in future:
        occurrence.status = OccurrenceStatus.CANCELLED.value
        occurrence.cancellation_reason = "Class batch deleted"
        from app.modules.communication.reminder_engine import handle_class_cancellation
        await handle_class_cancellation(db, occurrence.id, reason="Class batch deleted")
    await db.commit()

@router.get("/class-types")
async def list_class_types(
    org_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(ClassType).where(ClassType.organization_id == org_id, ClassType.deleted_at.is_(None))
    res = await db.execute(stmt)
    types = res.scalars().all()
    return [{"id": str(t.id), "name": t.name, "slug": t.slug, "description": t.description} for t in types]

@router.post("/class-types", status_code=status.HTTP_201_CREATED)
async def create_class_type(
    org_id: uuid.UUID,
    payload: ClassTypeCreate,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db)
):
    class_type = ClassType(
        organization_id=org_id,
        name=payload.name,
        slug=payload.slug,
        description=payload.description
    )
    db.add(class_type)
    await db.commit()
    await db.refresh(class_type)
    return {"id": str(class_type.id), "name": class_type.name, "slug": class_type.slug}

@router.get("/classes")
@router.get("/sessions", include_in_schema=False)
async def list_sessions(
    org_id: uuid.UUID,
    history: bool = False,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member),
    db: AsyncSession = Depends(get_db)
):
    # Reading history does not change any records.
    if not history:
        await auto_complete_expired_one_on_one_classes(db, org_id)

    stmt = select(Session).options(
        selectinload(Session.class_type),
        selectinload(Session.requirements),
        selectinload(Session.pricing),
        selectinload(Session.instructors).selectinload(SessionInstructor.member).selectinload(OrganizationMember.user),
        selectinload(Session.schedule_rules),
        selectinload(Session.occurrences)
    ).where(Session.organization_id == org_id, Session.deleted_at.is_not(None) if history else Session.deleted_at.is_(None))
    if tenant[1] and tenant[1].role == "TEACHER":
        stmt = stmt.where(Session.id.in_(select(SessionInstructor.session_id).where(SessionInstructor.member_id == tenant[1].id)))
    if history:
        stmt = stmt.order_by(Session.deleted_at.desc())
    res = await db.execute(stmt)
    sessions = res.scalars().all()
    appointment_ids = [s.id for s in sessions if s.session_type == "ONE_TO_ONE" and s.pricing and s.pricing.pricing_type == "PER_SESSION"]
    assignments = {}
    if appointment_ids:
        rows = (await db.execute(select(SessionEnrollment.session_id, User.id, User.full_name, User.email)
            .join(User, User.id == SessionEnrollment.user_id).where(SessionEnrollment.session_id.in_(appointment_ids),
                SessionEnrollment.organization_id == org_id, SessionEnrollment.status == "ACTIVE"))).all()
        for session_id, user_id, full_name, email in rows:
            assignments.setdefault(session_id, {"id": str(user_id), "name": full_name, "email": email})

    return [
        {
            "id": str(s.id),
            "name": s.name,
            "slug": s.slug,
            "short_description": s.short_description,
            "full_description": s.full_description,
            "class_type": s.class_type.name if s.class_type else None,
            "session_type": s.session_type,
            "skill_level": s.skill_level,
            "duration_minutes": s.duration_minutes,
            "language": s.language,
            "requirements": {key: getattr(s.requirements, key) for key in ("prerequisites", "required_equipment", "preparation_instructions", "additional_notes")} if s.requirements else None,
            "capacity": s.capacity,
            "is_active": s.is_active,
            "deleted_at": s.deleted_at.isoformat() if s.deleted_at else None,
            "is_public": s.is_public,
            "pricing": {
                "pricing_type": s.pricing.pricing_type,
                "price": float(s.pricing.price),
                "currency": s.pricing.currency,
                "total_classes": s.pricing.total_classes,
                "validity_days": s.pricing.validity_days,
                "packages": s.pricing.packages or []
            } if s.pricing else None,
            "instructors": [
                {
                    "id": str(i.member_id),
                    "member_id": str(i.member_id),
                    "full_name": i.member.user.full_name,
                    "role": i.role
                }
                for i in s.instructors
            ],
            "schedule_rules": [
                {
                    "id": str(r.id),
                    "day_of_week": r.day_of_week,
                    "start_time": r.start_time.isoformat(),
                    "end_time": r.end_time.isoformat(),
                    "timezone": r.timezone,
                    "effective_start_date": r.effective_start_date.isoformat() if r.effective_start_date else None,
                    "effective_end_date": r.effective_end_date.isoformat() if r.effective_end_date else None,
                    "is_active": r.is_active
                }
                for r in s.schedule_rules if r.is_active or history
            ],
            "assigned_student": assignments.get(s.id),
            "assigned_student_user_id": assignments.get(s.id, {}).get("id"),
            "occurrences": [
                {
                    "id": str(o.id),
                    "start_at": o.actual_start_at.isoformat(),
                    "end_at": o.actual_end_at.isoformat(),
                    "status": o.status,
                    "meeting_url": o.meeting_url
                }
                for o in s.occurrences
            ]
        }
        for s in sessions
    ]

@router.post("/one-on-one-classes", status_code=status.HTTP_201_CREATED)
async def create_one_on_one_class(
    org_id: uuid.UUID,
    payload: OneOnOneSessionCreate,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db)
):
    org, _ = tenant
    from app.modules.zoom.service import lock_studio
    await lock_studio(db, org_id)

    # Verify teacher belongs to this studio and is active
    teacher = await db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.id == payload.teacher_member_id,
            OrganizationMember.organization_id == org_id,
            OrganizationMember.is_active == True,
            OrganizationMember.can_teach.is_(True)
        )
    )
    if not teacher:
        raise HTTPException(422, "Selected teacher must be an active teacher of this studio")

    # Look up student by user_id or by OrganizationStudent id
    student_member = await db.scalar(
        select(OrganizationStudent).where(
            OrganizationStudent.organization_id == org_id,
            (OrganizationStudent.user_id == payload.student_user_id) | (OrganizationStudent.id == payload.student_user_id)
        )
    )
    if student_member:
        actual_student_user_id = student_member.user_id
    else:
        student_user = await db.scalar(
            select(User).where(
                User.id == payload.student_user_id,
                User.role == "STUDENT"
            )
        )
        if not student_user:
            raise HTTPException(422, "Selected student was not found")
        actual_student_user_id = student_user.id

    eligible_student = await db.scalar(select(User.id).where(
        User.id == actual_student_user_id, User.role == "STUDENT", User.is_verified.is_(True),
        User.is_active.is_(True), User.account_status == "ACTIVE", User.deleted_at.is_(None)))
    if not eligible_student:
        raise HTTPException(422, "Choose an active, verified student")

    if not student_member:
        db.add(OrganizationStudent(organization_id=org_id, user_id=actual_student_user_id, status="ACTIVE"))

    # Find or create a 1-on-1 ClassType
    class_type = await db.scalar(
        select(ClassType).where(
            ClassType.organization_id == org_id,
            ClassType.deleted_at.is_(None)
        ).order_by(ClassType.created_at.asc())
    )
    if not class_type:
        class_type = ClassType(
            organization_id=org_id,
            name="1-on-1 Private Session",
            slug=f"one-on-one-{uuid.uuid4().hex[:8]}",
            description="Private 1-on-1 sessions"
        )
        db.add(class_type)
        await db.flush()

    # Calculate duration and start/end datetimes
    st_parts = [int(x) for x in payload.start_time.split(":")[:2]]
    et_parts = [int(x) for x in payload.end_time.split(":")[:2]]
    duration_minutes = (et_parts[0] * 60 + et_parts[1]) - (st_parts[0] * 60 + st_parts[1])
    if duration_minutes <= 0:
        duration_minutes = 60

    try:
        org_tz = ZoneInfo(org.timezone or "Asia/Kolkata")
    except Exception:
        org_tz = ZoneInfo("Asia/Kolkata")

    st_time = time(st_parts[0], st_parts[1])
    et_time = time(et_parts[0], et_parts[1])
    start_dt = datetime.combine(payload.date, st_time, tzinfo=org_tz).astimezone(timezone.utc)
    end_dt = datetime.combine(payload.date, et_time, tzinfo=org_tz).astimezone(timezone.utc)

    # Generate meeting link
    meeting_url = None

    # Create Session (Capacity is strictly 1, non-repeating)
    session = Session(
        organization_id=org_id,
        class_type_id=class_type.id,
        name=payload.title,
        slug=f"1on1-{uuid.uuid4().hex[:8]}",
        short_description="1-on-1 Private Session",
        full_description=payload.full_description,
        session_type=SessionType.ONE_TO_ONE.value,
        capacity=1,
        skill_level=SkillLevel.ALL_LEVELS.value,
        duration_minutes=duration_minutes,
        is_active=True,
        is_public=False,
        meeting_url=meeting_url
    )
    db.add(session)
    await db.flush()

    # Add pricing
    pricing = SessionPricing(
        session_id=session.id,
        pricing_type=PricingType.PER_SESSION.value,
        price=payload.offer_price if payload.offer_price is not None else payload.price,
        currency="INR",
        total_classes=1,
        validity_days=7,
        packages=[{"id": str(uuid.uuid4()), "total_classes": 1, "price": float(payload.price),
                   "offer_price": float(payload.offer_price) if payload.offer_price is not None else None, "validity_days": 7}]
    )
    db.add(pricing)

    # Check teacher schedule conflict
    from app.modules.scheduling.engine import SchedulingEngine
    if await SchedulingEngine.check_instructor_conflict(db, payload.teacher_member_id, start_dt, end_dt):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Teacher has a scheduling conflict during this time slot. Please choose another time."
        )

    # Add primary instructor
    si = SessionInstructor(
        session_id=session.id,
        member_id=payload.teacher_member_id,
        role=InstructorRole.PRIMARY.value
    )
    db.add(si)

    # Add single occurrence
    now_utc = datetime.now(timezone.utc)
    is_already_ended = end_dt <= now_utc
    occ_status = OccurrenceStatus.COMPLETED.value if is_already_ended else OccurrenceStatus.SCHEDULED.value

    occ = ClassOccurrence(
        session_id=session.id,
        organization_id=org_id,
        instructor_id=payload.teacher_member_id,
        scheduled_start_at=start_dt,
        scheduled_end_at=end_dt,
        actual_start_at=start_dt,
        actual_end_at=end_dt,
        status=occ_status,
        meeting_url=meeting_url
    )
    db.add(occ)
    await db.flush()

    # Add enrollment for student
    enrollment = SessionEnrollment(
        organization_id=org_id,
        session_id=session.id,
        user_id=actual_student_user_id,
        status="ACTIVE",
        valid_from=start_dt,
        valid_until=end_dt + timedelta(days=1),
        total_sessions=1,
        sessions_attended=1 if is_already_ended else 0,
        billing_value=pricing.price,
        billing_currency=pricing.currency,
        billing_type=pricing.pricing_type,
        billing_label=session.name,
    )
    db.add(enrollment)
    await db.flush()

    # If the session has already concluded, immediately record attendance & soft-delete session
    if is_already_ended:
        att = AttendanceRecord(
            class_occurrence_id=occ.id,
            session_enrollment_id=enrollment.id,
            status=AttendanceStatus.ATTENDED.value,
            marked_at=occ.actual_end_at,
            notes="Automatically recorded upon 1-on-1 session completion"
        )
        db.add(att)
        session.deleted_at = now_utc
        session.is_active = False

    if not is_already_ended:
        from app.modules.communication.reminder_engine import sync_reminder_schedules_for_occurrence, queue_meeting_email
        await sync_reminder_schedules_for_occurrence(db, occ.id, start_dt)
        await queue_meeting_email(db, occ, "CLASS_INVITE", f"created:{occ.id}")

    await db.commit()
    await db.refresh(session)

    return {
        "id": str(session.id),
        "name": session.name,
        "occurrence_id": str(occ.id),
        "meeting_url": meeting_url,
        "start_at": start_dt.isoformat(),
        "end_at": end_dt.isoformat(),
        "is_completed": is_already_ended,
        "message": "1-on-1 Class scheduled successfully"
    }

@router.put("/one-on-one-classes/{class_id}")
async def update_one_on_one_class(
    org_id: uuid.UUID, class_id: uuid.UUID, payload: OneOnOneSessionCreate,
    tenant=Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db),
):
    from app.common.dates import ensure_utc
    from app.modules.zoom.service import lock_studio
    from app.modules.zoom.meetings import synchronize
    from app.modules.communication.reminder_engine import record_class_edit, queue_meeting_email
    await lock_studio(db, org_id)
    session = await db.scalar(select(Session).options(selectinload(Session.pricing)).where(
        Session.id == class_id, Session.organization_id == org_id, Session.deleted_at.is_(None)).with_for_update())
    if not session or session.session_type != "ONE_TO_ONE" or not session.pricing or session.pricing.pricing_type != "PER_SESSION":
        raise HTTPException(404, "Scheduled one-on-one class not found")
    occurrences = (await db.scalars(select(ClassOccurrence).where(ClassOccurrence.session_id == class_id).with_for_update())).all()
    if len(occurrences) != 1:
        raise HTTPException(409, "Use the batch editor for a reusable session package")
    occ = occurrences[0]
    now = datetime.now(timezone.utc)
    if occ.status not in {"SCHEDULED", "RESCHEDULED"} or ensure_utc(occ.actual_start_at) <= now:
        raise HTTPException(409, "Only an upcoming one-on-one class can be edited")
    teacher = await db.scalar(select(OrganizationMember).where(OrganizationMember.id == payload.teacher_member_id,
        OrganizationMember.organization_id == org_id, OrganizationMember.is_active.is_(True), OrganizationMember.can_teach.is_(True)))
    if not teacher:
        raise HTTPException(422, "Choose an active teacher of this studio")
    student = await db.scalar(select(User).where(User.id == payload.student_user_id, User.role == "STUDENT",
        User.is_active.is_(True), User.is_verified.is_(True), User.account_status == "ACTIVE", User.deleted_at.is_(None)))
    if not student:
        raise HTTPException(422, "Choose an active, verified student")
    zone = ZoneInfo(tenant[0].timezone or "Asia/Kolkata")
    start = datetime.combine(payload.date, time.fromisoformat(payload.start_time), zone).astimezone(timezone.utc)
    end = datetime.combine(payload.date, time.fromisoformat(payload.end_time), zone).astimezone(timezone.utc)
    if start <= now:
        raise HTTPException(422, "Choose a future start time")
    if await SchedulingEngine.check_instructor_conflict(db, teacher.id, start, end, exclude_occurrence_id=occ.id):
        raise HTTPException(409, "Teacher has a scheduling conflict during this time slot")
    if occ.zoom_connection_id and occ.instructor_id != teacher.id:
        raise HTTPException(409, "Reconcile the existing Zoom host in Zoom Settings before changing this teacher")
    old_start = occ.actual_start_at
    if occ.provider_meeting_id:
        await synchronize(db, occ, "reschedule", start=start, end=end, topic=payload.title)
    if ensure_utc(old_start) != start or ensure_utc(occ.actual_end_at) != end:
        occ.status = "RESCHEDULED"
    session.name, session.full_description = payload.title, payload.full_description
    session.duration_minutes = int((end - start).total_seconds() / 60)
    session.is_public, session.capacity = False, 1
    session.pricing.price = payload.offer_price if payload.offer_price is not None else payload.price
    option_id = (session.pricing.packages or [{}])[0].get("id", str(uuid.uuid4()))
    session.pricing.packages = [{"id": option_id, "total_classes": 1, "price": float(payload.price),
        "offer_price": float(payload.offer_price) if payload.offer_price is not None else None, "validity_days": 7}]
    await db.execute(delete(SessionInstructor).where(SessionInstructor.session_id == class_id))
    db.add(SessionInstructor(session_id=class_id, member_id=teacher.id, role="PRIMARY"))
    enrollments = (await db.scalars(select(SessionEnrollment).where(SessionEnrollment.session_id == class_id))).all()
    assigned = next((e for e in enrollments if e.user_id == student.id), None)
    for enrollment in enrollments:
        if enrollment is not assigned:
            if enrollment.status == "ACTIVE":
                await queue_meeting_email(db, occ, "CLASS_CANCELLATION", f"unassigned:{occ.id}:{uuid.uuid4()}",
                    recipient_id=enrollment.user_id, reason="Your assignment to this private appointment was cancelled by the studio.")
            enrollment.status = "CANCELLED"
    if not await db.scalar(select(OrganizationStudent.id).where(OrganizationStudent.organization_id == org_id, OrganizationStudent.user_id == student.id)):
        db.add(OrganizationStudent(organization_id=org_id, user_id=student.id, status="ACTIVE"))
    if assigned is None:
        assigned = SessionEnrollment(organization_id=org_id, session_id=class_id, user_id=student.id, total_sessions=1, sessions_attended=0)
        db.add(assigned)
    occ.instructor_id, occ.actual_start_at, occ.actual_end_at = teacher.id, start, end
    assigned.status, assigned.valid_from, assigned.valid_until = "ACTIVE", start, end + timedelta(days=1)
    assigned.billing_value, assigned.billing_currency, assigned.billing_type, assigned.billing_label = session.pricing.price, "INR", "PER_SESSION", session.name
    await db.flush()
    await record_class_edit(db, occ.id, {"actual_start_at": start, "actual_end_at": end, "instructor_id": teacher.id, "name": session.name}, {"actual_start_at": old_start})
    await queue_meeting_email(db, occ, "CLASS_INVITE", f"assigned:{occ.id}:{student.id}:{start.isoformat()}", recipient_id=student.id)
    await db.commit()
    return {"id": str(session.id), "occurrence_id": str(occ.id), "message": "One-on-one class updated"}

@router.post("/classes", status_code=status.HTTP_201_CREATED)
@router.post("/sessions", status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def create_session(
    org_id: uuid.UUID,
    payload: SessionCreate,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db)
):
    org, _ = tenant

    if not await db.scalar(select(ClassType.id).where(ClassType.id == payload.class_type_id, ClassType.organization_id == org_id)):
        raise HTTPException(422, "Class type must belong to this studio")
    for member_id in payload.instructor_member_ids:
        if not await db.scalar(select(OrganizationMember.id).where(OrganizationMember.id == member_id,
            OrganizationMember.organization_id == org_id, OrganizationMember.is_active == True,
            OrganizationMember.can_teach.is_(True))):
            raise HTTPException(422, "Teacher must belong to this studio")
    session = Session(
        organization_id=org_id,
        class_type_id=payload.class_type_id,
        name=payload.name,
        slug=payload.slug,
        short_description=payload.short_description,
        full_description=payload.full_description,
        session_type=payload.session_type.value,
        capacity=payload.capacity,
        min_age=payload.min_age,
        max_age=payload.max_age,
        skill_level=payload.skill_level.value,
        duration_minutes=payload.duration_minutes,
        language=payload.language,
        is_active=payload.is_active,
        is_public=False if payload.session_type == SessionType.ONE_TO_ONE and payload.pricing.pricing_type == PricingType.PER_SESSION and payload.pricing.total_classes == 1 else payload.is_public
    )
    db.add(session)
    await db.flush()

    # Add pricing
    pricing = SessionPricing(
        session_id=session.id,
        pricing_type=payload.pricing.pricing_type.value,
        price=payload.pricing.price,
        currency=payload.pricing.currency,
        total_classes=payload.pricing.total_classes,
        packages=[p.model_dump(mode="json") for p in payload.pricing.packages] if payload.pricing.packages else None,
        validity_days=payload.pricing.validity_days
    )
    db.add(pricing)

    # Add requirements
    if payload.requirements:
        reqs = SessionRequirement(
            session_id=session.id,
            prerequisites=payload.requirements.prerequisites,
            required_equipment=payload.requirements.required_equipment,
            preparation_instructions=payload.requirements.preparation_instructions,
            additional_notes=payload.requirements.additional_notes
        )
        db.add(reqs)

    # Add instructors
    for member_id in payload.instructor_member_ids:
        si = SessionInstructor(
            session_id=session.id,
            member_id=member_id,
            role="PRIMARY" if member_id == payload.instructor_member_ids[0] else "ASSISTANT"
        )
        db.add(si)

    # Add schedule rules
    for rule in payload.schedule_rules:
        st_parts = [int(x) for x in rule.start_time.split(":")]
        et_parts = [int(x) for x in rule.end_time.split(":")]
        sr = SessionScheduleRule(
            session_id=session.id,
            day_of_week=rule.day_of_week,
            start_time=time(st_parts[0], st_parts[1]),
            end_time=time(et_parts[0], et_parts[1]),
            timezone=org.timezone,
            effective_start_date=rule.effective_start_date,
            effective_end_date=rule.effective_end_date
        )
        db.add(sr)

    # Keep creation and scheduling together so a failed schedule does not leave
    # a saved class behind when the caller retries.
    await db.flush()
    await SchedulingEngine.generate_occurrences_for_session(db, session.id, days_ahead=30)
    await db.commit()
    await db.refresh(session)

    return {"id": str(session.id), "name": session.name, "slug": session.slug, "is_active": session.is_active}

@router.put("/classes/{class_id}")
@router.put("/sessions/{class_id}", include_in_schema=False)
async def update_session(
    org_id: uuid.UUID,
    class_id: uuid.UUID,
    payload: SessionUpdate,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.TEACHER])),
    db: AsyncSession = Depends(get_db)
):
    org, _ = tenant
    stmt = select(Session).options(
        selectinload(Session.pricing),
        selectinload(Session.instructors),
        selectinload(Session.schedule_rules),
        selectinload(Session.requirements)
    ).where(Session.id == class_id, Session.organization_id == org_id, Session.deleted_at.is_(None))
    res = await db.execute(stmt)
    session = res.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Class not found")

    if session.session_type == "ONE_TO_ONE" and session.pricing and session.pricing.pricing_type == "PER_SESSION" and session.pricing.total_classes == 1:
        raise HTTPException(409, "Use the one-on-one appointment editor to update this class")

    member = tenant[1]
    if member and member.role == "TEACHER":
        if not any(i.member_id == member.id for i in session.instructors):
            raise HTTPException(403, "You can only edit packages for your assigned classes")
        if payload.model_fields_set - {"pricing"}:
            raise HTTPException(403, "Teachers can edit class packages only")

    minimum = payload.min_age if "min_age" in payload.model_fields_set else session.min_age
    maximum = payload.max_age if "max_age" in payload.model_fields_set else session.max_age
    if minimum is not None and maximum is not None and minimum > maximum:
        raise HTTPException(422, "Minimum age must not exceed maximum age")
    for field in ("min_age", "max_age"):
        if field in payload.model_fields_set:
            setattr(session, field, getattr(payload, field))
    if payload.name is not None:
        session.name = payload.name
    if "capacity" in payload.model_fields_set:
        session.capacity = payload.capacity
    if payload.duration_minutes is not None:
        session.duration_minutes = payload.duration_minutes
    if payload.is_active is not None:
        session.is_active = payload.is_active
    if payload.is_public is not None:
        session.is_public = payload.is_public
    if payload.short_description is not None:
        session.short_description = payload.short_description
    if "full_description" in payload.model_fields_set:
        session.full_description = payload.full_description
    if payload.skill_level is not None:
        session.skill_level = payload.skill_level.value
    if payload.session_type is not None:
        session.session_type = payload.session_type.value
    if session.session_type == "ONE_TO_ONE" and session.pricing and session.pricing.pricing_type == "PER_SESSION" and session.pricing.total_classes == 1:
        session.is_public = False
    if payload.requirements is not None:
        from app.models.session import SessionRequirement
        if session.requirements:
            session.requirements.prerequisites = payload.requirements.prerequisites
            session.requirements.required_equipment = payload.requirements.required_equipment
            session.requirements.preparation_instructions = payload.requirements.preparation_instructions
            session.requirements.additional_notes = payload.requirements.additional_notes
        else:
            session.requirements = SessionRequirement(
                session_id=session.id,
                prerequisites=payload.requirements.prerequisites,
                required_equipment=payload.requirements.required_equipment,
                preparation_instructions=payload.requirements.preparation_instructions,
                additional_notes=payload.requirements.additional_notes
            )

    if payload.instructor_member_ids is not None:
        old_teachers = [i.member_id for i in session.instructors]
        old_primary = next((i.member_id for i in session.instructors if i.role == "PRIMARY"), None)
        if (set(old_teachers) != set(payload.instructor_member_ids) or old_primary != (payload.instructor_member_ids[0] if payload.instructor_member_ids else None)) and not getattr(payload, "acknowledge_zoom_host_impact", False) and await db.scalar(select(ClassOccurrence.id).where(
            ClassOccurrence.session_id == session.id, ClassOccurrence.actual_start_at > datetime.now(timezone.utc),
            ClassOccurrence.zoom_connection_id.is_not(None), ClassOccurrence.status.in_(["SCHEDULED", "RESCHEDULED"])).limit(1)):
            raise HTTPException(409, "Existing Zoom meetings retain their saved host. Reconcile individual class hosts in Zoom Settings before changing teacher assignments.")
        for member_id in payload.instructor_member_ids:
            if not await db.scalar(select(OrganizationMember.id).where(
                OrganizationMember.id == member_id,
                OrganizationMember.organization_id == org_id,
                OrganizationMember.is_active == True,
                OrganizationMember.can_teach.is_(True)
            )):
                raise HTTPException(422, "Teacher must belong to this studio")
        for ins in list(session.instructors):
            await db.delete(ins)
        for member_id in payload.instructor_member_ids:
            si = SessionInstructor(session_id=session.id, member_id=member_id, role="PRIMARY" if member_id == payload.instructor_member_ids[0] else "ASSISTANT")
            db.add(si)

        # Update instructor on future scheduled occurrences
        primary_member_id = payload.instructor_member_ids[0] if payload.instructor_member_ids else None
        now_utc = datetime.now(timezone.utc)
        await db.execute(
            update(ClassOccurrence)
            .where(
                ClassOccurrence.session_id == session.id,
                ClassOccurrence.status == OccurrenceStatus.SCHEDULED.value,
                ClassOccurrence.actual_start_at > now_utc
            )
            .values(instructor_id=primary_member_id)
        )

    if payload.pricing is not None:
        if session.pricing:
            session.pricing.pricing_type = payload.pricing.pricing_type.value
            session.pricing.price = payload.pricing.price
            session.pricing.currency = payload.pricing.currency
            session.pricing.total_classes = payload.pricing.total_classes
            session.pricing.validity_days = payload.pricing.validity_days
            if "packages" in payload.pricing.model_fields_set:
                session.pricing.packages = [p.model_dump(mode="json") for p in payload.pricing.packages] if payload.pricing.packages else None
        else:
            session.pricing = SessionPricing(
                session_id=session.id,
                pricing_type=payload.pricing.pricing_type.value,
                price=payload.pricing.price,
                currency=payload.pricing.currency,
                total_classes=payload.pricing.total_classes,
                packages=[p.model_dump(mode="json") for p in payload.pricing.packages] if payload.pricing.packages else None,
                validity_days=payload.pricing.validity_days
            )

    if payload.schedule_rules is not None:
        existing_rules = sorted([(r.day_of_week, r.start_time.isoformat(), r.end_time.isoformat(), str(r.effective_start_date), str(r.effective_end_date)) for r in session.schedule_rules], key=str)
        requested_rules = sorted([(r.day_of_week, time.fromisoformat(r.start_time).isoformat(), time.fromisoformat(r.end_time).isoformat(), str(r.effective_start_date), str(r.effective_end_date)) for r in payload.schedule_rules], key=str)
        if existing_rules != requested_rules and await db.scalar(select(ClassOccurrence.id).where(ClassOccurrence.session_id == session.id,
            ClassOccurrence.actual_start_at > datetime.now(timezone.utc), ClassOccurrence.zoom_connection_id.is_not(None),
            ClassOccurrence.status.in_(["SCHEDULED", "RESCHEDULED"])).limit(1)):
            raise HTTPException(409, "This timetable has provisioned Zoom meetings. Reschedule its existing occurrences explicitly before changing recurring rules.")
        for rule in list(session.schedule_rules):
            await db.delete(rule)
        for rule in payload.schedule_rules:
            st_parts = [int(x) for x in rule.start_time.split(":")]
            et_parts = [int(x) for x in rule.end_time.split(":")]
            sr = SessionScheduleRule(
                session_id=session.id,
                day_of_week=rule.day_of_week,
                start_time=time(st_parts[0], st_parts[1]),
                end_time=time(et_parts[0], et_parts[1]),
                timezone=org.timezone,
                effective_start_date=rule.effective_start_date,
                effective_end_date=rule.effective_end_date
            )
            db.add(sr)
        await SchedulingEngine.generate_occurrences_for_session(db, session.id, days_ahead=30)

    from app.modules.communication.reminder_engine import record_session_edit
    await record_session_edit(db, session.id, payload.model_dump(exclude_unset=True))
    await db.commit()
    await db.refresh(session)
    return {"id": str(session.id), "name": session.name, "is_active": session.is_active}

@router.patch("/classes/{class_id}/toggle-status")
@router.patch("/sessions/{class_id}/toggle-status", include_in_schema=False)
async def toggle_session_status(
    org_id: uuid.UUID,
    class_id: uuid.UUID,
    payload: SessionToggleStatus | None = None,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Session).where(Session.id == class_id, Session.organization_id == org_id, Session.deleted_at.is_(None))
    res = await db.execute(stmt)
    session = res.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Class not found")

    previous_active = session.is_active
    if payload and payload.is_active is not None:
        session.is_active = payload.is_active
    else:
        session.is_active = not session.is_active

    if session.is_active != previous_active:
        from app.modules.communication.reminder_engine import record_session_edit
        await record_session_edit(db, session.id, {"is_active": session.is_active})

    await db.commit()
    await db.refresh(session)
    return {"id": str(session.id), "is_active": session.is_active}
