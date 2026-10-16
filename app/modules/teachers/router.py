import math
import secrets
import uuid
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import select, or_, update, func, asc, desc
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import get_password_hash
from app.models.organization import OrganizationMember
from app.models.user import User
from app.models.session import Session, SessionInstructor
from app.modules.auth.dependencies import get_current_user
from app.modules.organizations.dependencies import resolve_staff_studio

router = APIRouter(prefix="/teachers", tags=["Studio Teachers Management"])

class TeacherCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    username: str | None = Field(None, min_length=3, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str | None = Field(None, min_length=8, max_length=128)
    phone: str | None = Field(None, max_length=50)
    address: str | None = Field(None, max_length=1000)
    location: str | None = Field(None, max_length=255)
    title: str = "Teacher"
    bio: str | None = None
    studio_id: uuid.UUID | None = None

class TeacherUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    email: EmailStr | None = None
    username: str | None = Field(None, min_length=3, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str | None = Field(None, min_length=8, max_length=128)
    phone: str | None = Field(None, max_length=50)
    address: str | None = Field(None, max_length=1000)
    location: str | None = Field(None, max_length=255)
    title: str | None = None
    bio: str | None = None
    is_active: bool | None = None


def serialize(member, user, org):
    return dict(id=str(member.id), user_id=str(user.id), name=user.full_name, email=user.email,
                username=user.username, phone=user.phone, address=user.address, location=user.location,
                title=member.title, bio=member.bio, role=user.role, studio_id=str(org.id), studio_name=org.name,
                is_active=member.is_active, status="ACTIVE" if member.is_active else "SUSPENDED",
                teacher_code=member.teacher_code, joined_at=member.created_at.isoformat())


async def generate_teacher_code(db: AsyncSession) -> str:
    for _ in range(20):
        code = f"{secrets.randbelow(90_000_000) + 10_000_000:08d}"
        if not await db.scalar(select(OrganizationMember.id).where(OrganizationMember.teacher_code == code)):
            return code
    raise HTTPException(503, "A teacher code could not be allocated. Please try again.")

@router.get("/public/lookup")
async def public_teacher_lookup(query: str = Query(..., min_length=1), db: AsyncSession = Depends(get_db)):
    q = query.strip()
    # Check if query matches a teacher code
    member = await db.scalar(
        select(OrganizationMember).where(OrganizationMember.teacher_code == q)
    )
    user = None
    if member:
        user = await db.get(User, member.user_id)
        if not member.is_active or (user and user.account_status != "ACTIVE"):
            raise HTTPException(404, "Teacher account is inactive")
    else:
        # Search by teacher full name
        user = await db.scalar(
            select(User).where(User.role == "TEACHER", User.full_name.ilike(f"%{q}%"))
        )
        if user and user.account_status != "ACTIVE":
            raise HTTPException(404, "Teacher account is inactive")
        if not user:
            raise HTTPException(404, "Teacher not found. Check the code or name and try again.")
        # Check active memberships
        active_members = (await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.user_id == user.id,
                OrganizationMember.role == "TEACHER",
                OrganizationMember.is_active.is_(True)
            )
        )).scalars().all()
        if not active_members:
            # Check if inactive membership exists
            has_member = await db.scalar(
                select(OrganizationMember.id).where(OrganizationMember.user_id == user.id, OrganizationMember.role == "TEACHER")
            )
            if has_member:
                raise HTTPException(404, "Teacher account is inactive")
            raise HTTPException(404, "Teacher not found. Check the code or name and try again.")
        member = active_members[0]

    if not user:
        raise HTTPException(404, "Teacher not found. Check the code or name and try again.")

    from datetime import datetime, timezone
    from app.models.organization import Organization
    from app.models.commerce import SessionEnrollment
    from app.models.schedule import ClassOccurrence
    from app.models.membership_tier import MembershipTier, MembershipTierClass
    from sqlalchemy.orm import selectinload

    now_utc = datetime.now(timezone.utc)

    # Fetch all studio affiliations for this teacher
    affiliations = (await db.execute(
        select(OrganizationMember, Organization)
        .join(Organization, OrganizationMember.organization_id == Organization.id)
        .where(
            OrganizationMember.user_id == user.id,
            OrganizationMember.role == "TEACHER",
            OrganizationMember.is_active.is_(True),
            Organization.status == "ACTIVE",
            Organization.deleted_at.is_(None)
        )
        .order_by(OrganizationMember.created_at.asc())
    )).all()

    studios_data = []
    for m, org in affiliations:
        # Sessions taught by this teacher in this studio
        instruct_sessions = (await db.execute(
            select(Session)
            .join(SessionInstructor, SessionInstructor.session_id == Session.id)
            .options(
                selectinload(Session.pricing),
                selectinload(Session.schedule_rules)
            )
            .where(
                SessionInstructor.member_id == m.id,
                Session.organization_id == org.id,
                Session.is_active.is_(True),
                Session.is_public.is_(True),
                Session.deleted_at.is_(None)
            )
            .order_by(Session.name.asc())
        )).scalars().all()

        sessions_list = []
        for s in instruct_sessions:
            enrolled_count = await db.scalar(
                select(func.count(SessionEnrollment.id)).where(
                    SessionEnrollment.session_id == s.id,
                    SessionEnrollment.status == "ACTIVE"
                )
            ) or 0
            remaining_spots = max(0, s.capacity - enrolled_count)

            # Next session occurrence
            next_occ = await db.scalar(
                select(ClassOccurrence).where(
                    ClassOccurrence.session_id == s.id,
                    ClassOccurrence.status == "SCHEDULED",
                    ClassOccurrence.actual_start_at >= now_utc
                ).order_by(ClassOccurrence.actual_start_at.asc()).limit(1)
            )

            sessions_list.append({
                "id": str(s.id),
                "name": s.name,
                "slug": s.slug,
                "short_description": s.short_description,
                "session_type": s.session_type,
                "delivery_mode": "ONLINE_LIVE" if s.session_type in ("ONLINE", "LIVE") else "HYBRID",
                "skill_level": s.skill_level,
                "duration_minutes": s.duration_minutes,
                "capacity": s.capacity,
                "remaining_spots": remaining_spots,
                "pricing": {
                    "pricing_type": s.pricing.pricing_type,
                    "price": float(s.pricing.price),
                    "currency": s.pricing.currency,
                    "total_classes": s.pricing.total_classes if s.pricing.total_classes is not None else (1 if s.pricing.pricing_type in ("ONE_TIME", "PER_SESSION") else None),
                    "validity_days": s.pricing.validity_days or 30
                } if s.pricing else None,
                "next_session": {
                    "id": str(next_occ.id),
                    "start_time": next_occ.actual_start_at.isoformat(),
                    "end_time": next_occ.actual_end_at.isoformat(),
                    "timezone": org.timezone
                } if next_occ else None,
                "schedule": [
                    {
                        "day_of_week": r.day_of_week,
                        "start_time": r.start_time.isoformat(),
                        "end_time": r.end_time.isoformat()
                    }
                    for r in s.schedule_rules if r.is_active
                ]
            })

        # Studio passes
        tiers = (await db.execute(
            select(MembershipTier).options(
                selectinload(MembershipTier.classes).selectinload(MembershipTierClass.session)
            ).where(
                MembershipTier.organization_id == org.id,
                MembershipTier.is_active.is_(True),
                MembershipTier.deleted_at.is_(None)
            ).order_by(MembershipTier.name)
        )).scalars().all()

        teacher_session_ids = {s.id for s in instruct_sessions}
        passes_list = [
            {
                "id": str(t.id),
                "name": t.name,
                "description": t.description,
                "price": float(t.price),
                "offer_price": float(t.offer_price) if t.offer_price is not None else None,
                "currency": t.currency,
                "tier_type": t.tier_type,
                "total_classes": t.total_classes,
                "validity_days": t.validity_days or 30
            }
            for t in tiers
            if not t.classes or any(tc.session_id in teacher_session_ids for tc in t.classes)
        ]

        studios_data.append({
            "studio_id": str(org.id),
            "studio_name": org.name,
            "studio_slug": org.slug,
            "location": getattr(org, "location", None) or f"{org.name} Studio",
            "timezone": org.timezone,
            "teacher_code": m.teacher_code,
            "classes": sessions_list,
            "passes": passes_list
        })

    return {
        "teacher": {
            "id": str(user.id),
            "name": user.full_name,
            "teacher_code": member.teacher_code if member else None,
            "title": member.title if member else "Teacher",
            "bio": member.bio if member else None,
            "avatar_url": user.avatar_url,
            "is_active": True
        },
        "studios": studios_data
    }

@router.get("/public/{code_or_id}")
async def public_teacher_profile(code_or_id: str, db: AsyncSession = Depends(get_db)):
    return await public_teacher_lookup(query=code_or_id, db=db)

@router.get("")
async def list_studio_teachers(studio_id: str | None = None, search: str | None = None,
                              page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100),
                              sort_by: Literal["name", "code", "joined", "status"] = "name",
                              sort_order: Literal["asc", "desc"] = "asc",
                              current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    org, _ = await resolve_staff_studio(db, current_user, studio_id, owner_only=True)
    filters = [OrganizationMember.organization_id == org.id, OrganizationMember.role == "TEACHER"]
    if search:
        filters.append(or_(User.full_name.ilike(f"%{search}%"), User.email.ilike(f"%{search}%"),
                           OrganizationMember.teacher_code.ilike(f"%{search}%")))
    stmt = select(OrganizationMember, User).join(User).where(*filters)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    active_total = await db.scalar(select(func.count(OrganizationMember.id)).join(User).where(
        *filters, OrganizationMember.is_active.is_(True))) or 0
    sort_column = {"name": User.full_name, "code": OrganizationMember.teacher_code,
                   "joined": OrganizationMember.created_at, "status": OrganizationMember.is_active}[sort_by]
    ordering = desc(sort_column) if sort_order == "desc" else asc(sort_column)
    rows = (await db.execute(stmt.order_by(ordering, OrganizationMember.id)
                             .offset((page - 1) * page_size).limit(page_size))).all()
    return {"teachers": [serialize(m, u, org) for m, u in rows], "total": total,
            "active_total": active_total,
            "page": page, "page_size": page_size, "total_pages": max(1, math.ceil(total / page_size))}

@router.post("", status_code=201)
async def create_studio_teacher(payload: TeacherCreateRequest, current_user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    org, _ = await resolve_staff_studio(db, current_user, payload.studio_id, owner_only=True)
    email = payload.email.lower().strip()
    username = payload.username.lower().strip() if payload.username else f"{email.split('@')[0]}_{secrets.token_hex(3)}"
    password = payload.password if payload.password else secrets.token_urlsafe(16)
    phone = payload.phone or "+91 98000 00000"
    address = payload.address or "Studio Campus"
    location = payload.location or "Sanctuary"

    if await db.scalar(select(User.id).where(or_(User.email == email, User.username == username))):
        raise HTTPException(409, "Email or username is already in use")
    user = User(email=email, username=username, full_name=payload.name.strip(), role="TEACHER",
                password_hash=get_password_hash(password), phone=phone,
                address=address, location=location, is_verified=True, account_status="ACTIVE")
    db.add(user)
    await db.flush()
    member = OrganizationMember(organization_id=org.id, user_id=user.id, role="TEACHER",
                                title=payload.title, bio=payload.bio,
                                teacher_code=await generate_teacher_code(db))
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return serialize(member, user, org)

async def owned_teacher(db, user, member_id):
    row = (await db.execute(select(OrganizationMember, User).join(User).where(
        OrganizationMember.id == member_id, OrganizationMember.role == "TEACHER"))).first()
    if not row:
        raise HTTPException(404, "Teacher not found")
    org, _ = await resolve_staff_studio(db, user, row[0].organization_id, owner_only=True)
    return row[0], row[1], org

@router.put("/{member_id}")
async def update_studio_teacher(member_id: uuid.UUID, payload: TeacherUpdateRequest,
                                current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    member, user, org = await owned_teacher(db, current_user, member_id)
    values = payload.model_dump(exclude_unset=True, exclude_none=True)
    for key in ("email", "username"):
        if key in values:
            values[key] = values[key].lower()
            if await db.scalar(select(User.id).where(getattr(User, key) == values[key], User.id != user.id)):
                raise HTTPException(409, f"{key.title()} is already in use")
    deactivate_assigned_classes = values.get("is_active") is False and member.is_active
    for key, value in values.items():
        if key == "password":
            user.password_hash = get_password_hash(value)
        elif key == "name":
            user.full_name = value.strip()
        elif key in {"title", "bio", "is_active"}:
            setattr(member, key, value)
        else:
            setattr(user, key, value)
    deactivated_class_count = 0
    if deactivate_assigned_classes:
        deactivated = await db.execute(
            update(Session)
            .where(Session.id.in_(select(SessionInstructor.session_id).where(SessionInstructor.member_id == member.id)),
                   Session.organization_id == org.id, Session.is_active == True)
            .values(is_active=False)
        )
        deactivated_class_count = deactivated.rowcount or 0
    await db.commit()
    result = serialize(member, user, org)
    result["deactivated_class_count"] = deactivated_class_count
    return result

@router.delete("/{member_id}")
async def remove_studio_teacher(member_id: uuid.UUID, current_user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    member, user, org = await owned_teacher(db, current_user, member_id)
    was_active = member.is_active
    member.is_active = False
    if was_active:
        await db.execute(
            update(Session)
            .where(Session.id.in_(select(SessionInstructor.session_id).where(SessionInstructor.member_id == member.id)),
                   Session.organization_id == org.id, Session.is_active == True)
            .values(is_active=False)
        )
    await db.commit()
    return {"message": "Teacher access and assigned classes deactivated; attendance history retained"}
