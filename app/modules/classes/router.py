import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import secrets
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
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
    session = await db.scalar(select(Session).where(
        Session.id == class_id, Session.organization_id == org_id, Session.deleted_at.is_(None)))
    if not session:
        raise HTTPException(404, "Class not found")

    now = datetime.now(timezone.utc)
    session.deleted_at = now
    session.is_active = False
    await db.execute(update(SessionScheduleRule).where(
        SessionScheduleRule.session_id == class_id).values(is_active=False))
    await db.execute(update(ClassOccurrence).where(
        ClassOccurrence.session_id == class_id,
        ClassOccurrence.status == OccurrenceStatus.SCHEDULED.value,
        ClassOccurrence.actual_start_at >= now,
    ).values(status=OccurrenceStatus.CANCELLED.value, cancellation_reason="Class batch deleted"))
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

    return [
        {
            "id": str(s.id),
            "name": s.name,
            "slug": s.slug,
            "short_description": s.short_description,
            "class_type": s.class_type.name if s.class_type else None,
            "session_type": s.session_type,
            "skill_level": s.skill_level,
            "duration_minutes": s.duration_minutes,
            "capacity": s.capacity,
            "is_active": s.is_active,
            "deleted_at": s.deleted_at.isoformat() if s.deleted_at else None,
            "is_public": s.is_public,
            "pricing": {
                "pricing_type": s.pricing.pricing_type,
                "price": float(s.pricing.price),
                "currency": s.pricing.currency
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

    # Verify teacher belongs to this studio and is active
    teacher = await db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.id == payload.teacher_member_id,
            OrganizationMember.organization_id == org_id,
            OrganizationMember.is_active == True,
            OrganizationMember.role.in_(["STUDIO", "TEACHER"])
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
    meeting_url = f"{settings.CLASS_MEETING_BASE_URL.rstrip('/')}/ProgressRooms-{secrets.token_urlsafe(24)}"

    # Create Session (Capacity is strictly 1, non-repeating)
    session = Session(
        organization_id=org_id,
        class_type_id=class_type.id,
        name=payload.title,
        slug=f"1on1-{uuid.uuid4().hex[:8]}",
        short_description="1-on-1 Private Session",
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
        price=payload.price,
        currency="INR",
        total_classes=1,
        validity_days=7
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
            OrganizationMember.role.in_(["STUDIO", "TEACHER"]))):
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
        is_public=payload.is_public
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
            role="PRIMARY"
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

    await db.commit()
    await db.refresh(session)

    # Proactively generate rolling occurrences for 30 days
    await SchedulingEngine.generate_occurrences_for_session(db, session.id, days_ahead=30)

    return {"id": str(session.id), "name": session.name, "slug": session.slug, "is_active": session.is_active}

@router.put("/classes/{class_id}")
@router.put("/sessions/{class_id}", include_in_schema=False)
async def update_session(
    org_id: uuid.UUID,
    class_id: uuid.UUID,
    payload: SessionUpdate,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db)
):
    org, _ = tenant
    stmt = select(Session).options(
        selectinload(Session.pricing),
        selectinload(Session.instructors),
        selectinload(Session.schedule_rules)
    ).where(Session.id == class_id, Session.organization_id == org_id, Session.deleted_at.is_(None))
    res = await db.execute(stmt)
    session = res.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Class not found")

    if payload.name is not None:
        session.name = payload.name
    if payload.capacity is not None:
        session.capacity = payload.capacity
    if payload.duration_minutes is not None:
        session.duration_minutes = payload.duration_minutes
    if payload.is_active is not None:
        session.is_active = payload.is_active
    if payload.is_public is not None:
        session.is_public = payload.is_public
    if payload.short_description is not None:
        session.short_description = payload.short_description
    if payload.full_description is not None:
        session.full_description = payload.full_description
    if payload.skill_level is not None:
        session.skill_level = payload.skill_level.value
    if payload.session_type is not None:
        session.session_type = payload.session_type.value
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
        for member_id in payload.instructor_member_ids:
            if not await db.scalar(select(OrganizationMember.id).where(
                OrganizationMember.id == member_id,
                OrganizationMember.organization_id == org_id,
                OrganizationMember.is_active == True,
                OrganizationMember.role.in_(["STUDIO", "TEACHER"])
            )):
                raise HTTPException(422, "Teacher must belong to this studio")
        for ins in list(session.instructors):
            await db.delete(ins)
        for member_id in payload.instructor_member_ids:
            si = SessionInstructor(session_id=session.id, member_id=member_id, role="PRIMARY")
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
        else:
            session.pricing = SessionPricing(
                session_id=session.id,
                pricing_type=payload.pricing.pricing_type.value,
                price=payload.pricing.price,
                currency=payload.pricing.currency,
                total_classes=payload.pricing.total_classes,
                validity_days=payload.pricing.validity_days
            )

    if payload.schedule_rules is not None:
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

    if payload and payload.is_active is not None:
        session.is_active = payload.is_active
    else:
        session.is_active = not session.is_active

    await db.commit()
    await db.refresh(session)
    return {"id": str(session.id), "is_active": session.is_active}
