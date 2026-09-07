import uuid
from datetime import datetime, time
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember
from app.models.session import ClassType, Session, SessionInstructor, SessionPricing, SessionRequirement
from app.models.schedule import SessionScheduleRule
from app.modules.organizations.dependencies import get_current_tenant_member, require_org_role
from app.modules.classes.schemas import ClassTypeCreate, SessionCreate
from app.common.enums import UserRole
from app.modules.scheduling.engine import SchedulingEngine

router = APIRouter(prefix="/organizations/{org_id}", tags=["Classes & Sessions"])

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

@router.get("/sessions")
async def list_sessions(
    org_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Session).options(
        selectinload(Session.class_type),
        selectinload(Session.pricing),
        selectinload(Session.instructors).selectinload(SessionInstructor.member).selectinload(OrganizationMember.user),
        selectinload(Session.schedule_rules)
    ).where(Session.organization_id == org_id, Session.deleted_at.is_(None))
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
            "is_public": s.is_public,
            "pricing": {
                "pricing_type": s.pricing.pricing_type,
                "price": float(s.pricing.price),
                "currency": s.pricing.currency
            } if s.pricing else None,
            "instructors": [
                {
                    "member_id": str(i.member_id),
                    "full_name": i.member.user.full_name,
                    "role": i.role
                }
                for i in s.instructors
            ],
            "schedule_rules": [
                {
                    "day_of_week": r.day_of_week,
                    "start_time": r.start_time.isoformat(),
                    "end_time": r.end_time.isoformat()
                }
                for r in s.schedule_rules if r.is_active
            ]
        }
        for s in sessions
    ]

@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    org_id: uuid.UUID,
    payload: SessionCreate,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db)
):
    org, _ = tenant

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

    return {"id": str(session.id), "name": session.name, "slug": session.slug}
