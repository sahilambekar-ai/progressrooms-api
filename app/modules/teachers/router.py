from app.core.password_work import hash_password, run
from app.common.profile_consent import attest_profile_change
import math
import secrets
import uuid
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import select, or_, update, func, asc, desc
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.common.email_content import email_subject
from app.common.validation import Name
from app.modules.auth.sessions import revoke_sessions
from app.modules.teachers.credentials import reconfirm, seal, reveal
from app.modules.teachers.codes import generate_teacher_code
from app.modules.teachers.service import protect_teaching_roster
from app.models.organization import OrganizationMember
from app.models.user import User
from app.models.session import Session, SessionInstructor
from app.modules.auth.dependencies import get_current_user
from app.modules.organizations.dependencies import resolve_staff_studio

router = APIRouter(prefix="/teachers", tags=["Studio Teachers Management"])

class TeacherCreateRequest(BaseModel):
    billing_country: str | None = Field(None, pattern=r"^[A-Z]{2}$")
    name: Name
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
    profile_consent_confirmed: bool = False
    profile_consent_reference: str | None = Field(None, min_length=1, max_length=255)
    billing_country: str | None = Field(None, pattern=r"^[A-Z]{2}$")
    studio_password: str = Field(default="", max_length=128)
    name: Name | None = None
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
                username=user.username, phone=user.phone, address=user.address, location=user.location, billing_country=user.billing_country,
                title=member.title, bio=member.bio, role=member.role,
                is_owner=member.role == "STUDIO", can_teach=member.can_teach, studio_id=str(org.id), studio_name=org.name,
                is_active=member.is_active, status="ACTIVE" if member.is_active else "SUSPENDED",
                teacher_code=member.teacher_code, joined_at=member.created_at.isoformat())


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
        if not member.can_teach or not member.is_active or (user and user.account_status != "ACTIVE"):
            raise HTTPException(404, "Teacher account is inactive")
    else:
        # Search by teacher full name
        user = await db.scalar(
            select(User).join(OrganizationMember).where(OrganizationMember.can_teach.is_(True), OrganizationMember.is_active.is_(True), User.full_name.ilike(f"%{q}%"))
        )
        if user and user.account_status != "ACTIVE":
            raise HTTPException(404, "Teacher account is inactive")
        if not user:
            raise HTTPException(404, "Teacher not found. Check the code or name and try again.")
        # Check active memberships
        active_members = (await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.user_id == user.id,
                OrganizationMember.can_teach.is_(True),
                OrganizationMember.is_active.is_(True)
            )
        )).scalars().all()
        if not active_members:
            # Check if inactive membership exists
            has_member = await db.scalar(
                select(OrganizationMember.id).where(OrganizationMember.user_id == user.id, OrganizationMember.can_teach.is_(True))
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
            OrganizationMember.can_teach.is_(True),
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
                selectinload(Session.instructors).selectinload(SessionInstructor.member).selectinload(OrganizationMember.user),
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

        ids = [course.id for course in instruct_sessions]
        enrollment_counts = dict((await db.execute(select(SessionEnrollment.session_id, func.count(SessionEnrollment.id)).where(
            SessionEnrollment.organization_id == org.id, SessionEnrollment.session_id.in_(ids),
            SessionEnrollment.status == "ACTIVE").group_by(SessionEnrollment.session_id))).all()) if ids else {}
        ranked = select(ClassOccurrence.id.label("id"), func.row_number().over(
            partition_by=ClassOccurrence.session_id,
            order_by=(ClassOccurrence.actual_start_at.asc(), ClassOccurrence.id.asc())).label("position")).where(
                ClassOccurrence.organization_id == org.id, ClassOccurrence.session_id.in_(ids),
                ClassOccurrence.status == "SCHEDULED", ClassOccurrence.actual_start_at >= now_utc).subquery()
        next_occurrences = {row.session_id: row for row in (await db.scalars(select(ClassOccurrence).join(
            ranked, ranked.c.id == ClassOccurrence.id).where(ranked.c.position == 1))).all()} if ids else {}
        sessions_list = []
        for s in instruct_sessions:
            enrolled_count = enrollment_counts.get(s.id, 0)
            remaining_spots = max(0, s.capacity - enrolled_count) if s.capacity is not None else None
            next_occ = next_occurrences.get(s.id)

            sessions_list.append({
                "id": str(s.id),
                "name": s.name,
                "slug": s.slug,
                "short_description": s.short_description,
                "full_description": s.full_description,
                "session_type": s.session_type,
                "delivery_mode": "ONLINE_LIVE" if s.session_type in ("ONLINE", "LIVE") else "HYBRID",
                "skill_level": s.skill_level,
                "duration_minutes": s.duration_minutes,
                "instructor_names": [i.member.user.full_name for i in s.instructors if i.member and i.member.user],
                "capacity": s.capacity,
                "remaining_spots": remaining_spots,
                "pricing": {
                    "packages": s.pricing.packages or [],
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
                "class_packages": t.class_packages or [],
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
    filters = [OrganizationMember.organization_id == org.id, OrganizationMember.can_teach.is_(True)]
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
    from app.modules.zoom.service import teacher_state
    return {"teachers": [dict(serialize(m, u, org), **await teacher_state(db, m)) for m, u in rows], "total": total,
            "active_total": active_total,
            "page": page, "page_size": page_size, "total_pages": max(1, math.ceil(total / page_size))}

@router.post("", status_code=201)
async def create_studio_teacher(payload: TeacherCreateRequest, current_user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    org, _ = await resolve_staff_studio(db, current_user, payload.studio_id, owner_only=True)
    email = payload.email.lower().strip()
    username = payload.username.lower().strip() if payload.username else f"{email.split('@')[0]}_{secrets.token_hex(3)}"
    password = payload.password if payload.password else secrets.token_urlsafe(16)
    phone = payload.phone
    address = payload.address
    location = payload.location

    if await db.scalar(select(User.id).where(or_(User.email == email, User.username == username))):
        raise HTTPException(409, "Email or username is already in use")
    user = User(email=email, username=username, full_name=payload.name.strip(), role="TEACHER",
                password_hash=await hash_password(password), managed_password_enc=seal(password), phone=phone,
                address=address, location=location, billing_country=payload.billing_country, is_verified=True, account_status="ACTIVE")
    db.add(user)
    await db.flush()
    member = OrganizationMember(organization_id=org.id, user_id=user.id, role="TEACHER",
                                title=payload.title, bio=payload.bio, can_teach=True,
                                teacher_code=await generate_teacher_code(db))
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return serialize(member, user, org)

async def owned_teacher(db, user, member_id):
    row = (await db.execute(select(OrganizationMember, User).join(User).where(
        OrganizationMember.id == member_id, OrganizationMember.can_teach.is_(True)))).first()
    if not row:
        raise HTTPException(404, "Teacher not found")
    org, _ = await resolve_staff_studio(db, user, row[0].organization_id, owner_only=True)
    return row[0], row[1], org

@router.put("/{member_id}")
async def update_studio_teacher(member_id: uuid.UUID, payload: TeacherUpdateRequest,
                                current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    member, user, org = await owned_teacher(db, current_user, member_id)
    await reconfirm(current_user, payload.studio_password)
    values = payload.model_dump(exclude_unset=True, exclude_none=True, exclude={"studio_password", "profile_consent_confirmed", "profile_consent_reference"})
    for key in ("email", "username"):
        if key in values:
            values[key] = values[key].lower()
            if await db.scalar(select(User.id).where(getattr(User, key) == values[key], User.id != user.id)):
                raise HTTPException(409, f"{key.title()} is already in use")
    changed = [key for key in ("name", "email", "username", "phone", "address", "location", "billing_country")
               if key in values and (getattr(user, "full_name" if key == "name" else key) or None) != (values[key] or None)]
    attest_profile_change(db, actor=current_user, subject=user, organization_id=org.id,
        changed_fields=changed, confirmed=payload.profile_consent_confirmed, reference=payload.profile_consent_reference)
    if values.get("is_active") is False:
        await protect_teaching_roster(db, member)
    deactivate_assigned_classes = values.get("is_active") is False and member.is_active
    for key, value in values.items():
        if key == "password":
            user.password_hash = await hash_password(value)
            await revoke_sessions(db, user)
            user.managed_password_enc = seal(value)
        elif key == "name":
            user.full_name = value.strip()
        elif key in {"title", "bio", "is_active"}:
            setattr(member, key, value)
        else:
            setattr(user, key, value)
    deactivated_class_count = 0
    if deactivate_assigned_classes:
        from app.modules.zoom.service import disconnect
        await disconnect(db, member, pause_confirmed=True, commit=False)
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
    if user.teacher_invitation_fingerprint:
        try:
            delivery = await invite_teacher(member_id, CredentialRequest(studio_password=payload.studio_password), current_user, db)
            result["invitation_message"] = delivery["message"]
        except HTTPException as exc:
            result["invitation_message"] = f"Teacher saved. {exc.detail}"
    return result

@router.delete("/{member_id}")
async def remove_studio_teacher(member_id: uuid.UUID, current_user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    member, user, org = await owned_teacher(db, current_user, member_id)
    await protect_teaching_roster(db, member)
    was_active = member.is_active
    from app.modules.zoom.service import disconnect
    await disconnect(db, member, pause_confirmed=True, commit=False)
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


class CredentialRequest(BaseModel):
    studio_password: str = Field(min_length=1, max_length=128)

@router.post("/{member_id}/password")
async def teacher_password(member_id: uuid.UUID, payload: CredentialRequest,
                           current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from fastapi.responses import JSONResponse
    _, user, _ = await owned_teacher(db, current_user, member_id)
    await reconfirm(current_user, payload.studio_password)
    return JSONResponse({"password": await run(reveal, user)}, headers={"Cache-Control": "no-store"})

@router.post("/{member_id}/invitation")
async def invite_teacher(member_id: uuid.UUID, payload: CredentialRequest,
                         current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    import hashlib
    import json
    from html import escape
    import httpx
    from app.core.config import settings
    from app.core.email_service import BREVO_API_URL
    member, user, org = await owned_teacher(db, current_user, member_id)
    await reconfirm(current_user, payload.studio_password)
    # Lock across dispatch so repeated clicks cannot send duplicate invitations.
    user = await db.scalar(select(User).where(User.id == user.id).with_for_update())
    details = serialize(member, user, org)
    details["password"] = await run(reveal, user)
    fingerprint = hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()
    if user.teacher_invitation_fingerprint == fingerprint:
        return {"message": "Invitation already sent; no teacher details have changed"}
    if not settings.BREVO_ENABLED or not settings.BREVO_API_KEY:
        raise HTTPException(503, "Email delivery is not configured; teacher saved, invitation not sent")
    content = "<h2>Your Studio teacher account</h2>" + "".join(
        f"<p><b>{escape(k.replace('_', ' ').title())}:</b> {escape(str(v))}</p>"
        for k, v in details.items() if v is not None)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(BREVO_API_URL, headers={"api-key": settings.BREVO_API_KEY}, json={
                "sender": {"name": settings.BREVO_SENDER_NAME, "email": settings.BREVO_SENDER_EMAIL},
                "to": [{"email": user.email, "name": user.full_name}],
                "subject": email_subject(f"Your teacher account at {org.name}"), "htmlContent": content})
        if response.status_code not in (200, 201):
            raise HTTPException(502, "Teacher saved but invitation delivery failed; retry sending")
    except httpx.RequestError:
        raise HTTPException(502, "Invitation delivery unavailable; retry sending")
    user.teacher_invitation_fingerprint = fingerprint
    await db.commit()
    return {"message": "Teacher invitation sent by email"}
