import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember
from app.models.user import User

router = APIRouter(prefix="/teachers", tags=["Studio Teachers Management"])

class TeacherCreateRequest(BaseModel):
    name: str
    email: str
    phone: Optional[str] = None
    title: str = "Instructor"
    bio: Optional[str] = "Certified instructor and sanctuary guide."
    studio_id: Optional[str] = None

class TeacherUpdateRequest(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    title: Optional[str] = None
    bio: Optional[str] = None
    is_active: Optional[bool] = None

@router.get("")
async def list_studio_teachers(
    studio_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """List all teachers in the studio."""
    stmt = (
        select(OrganizationMember, User, Organization)
        .join(User, User.id == OrganizationMember.user_id)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(OrganizationMember.role.in_(["INSTRUCTOR", "OWNER"]))
    )

    if studio_id:
        try:
            org_uuid = uuid.UUID(studio_id)
            stmt = stmt.where(Organization.id == org_uuid)
        except ValueError:
            stmt = stmt.where(Organization.slug == studio_id)

    if search:
        st = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(User.full_name).like(st),
                func.lower(User.email).like(st),
                func.lower(OrganizationMember.title).like(st)
            )
        )

    stmt = stmt.order_by(OrganizationMember.created_at.asc())
    result = await db.execute(stmt)
    rows = result.all()

    teachers = []
    for member, user, org in rows:
        teachers.append({
            "id": str(member.id),
            "user_id": str(user.id),
            "name": user.full_name,
            "email": user.email,
            "phone": user.phone or "+91 98765 00000",
            "title": member.title or "Instructor",
            "bio": member.bio or "Certified somatic instructor.",
            "studio_id": str(org.id),
            "studio_name": org.name,
            "role": member.role,
            "is_active": member.is_active,
            "status": "ACTIVE" if member.is_active else "SUSPENDED",
            "joined_at": member.created_at.strftime("%b %d, %Y") if member.created_at else "Recently"
        })

    return {"teachers": teachers, "total": len(teachers)}

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_studio_teacher(
    payload: TeacherCreateRequest,
    db: AsyncSession = Depends(get_db)
):
    """Add a new teacher to the studio."""
    clean_email = payload.email.strip().lower()

    # Find or create user
    user_stmt = select(User).where(User.email == clean_email)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()

    if not user:
        user = User(
            email=clean_email,
            full_name=payload.name.strip(),
            phone=payload.phone.strip() if payload.phone else "+91 98765 11223",
            is_verified=True,
            account_status="ACTIVE"
        )
        db.add(user)
        await db.flush()
    else:
        user.full_name = payload.name.strip()
        if payload.phone:
            user.phone = payload.phone.strip()

    # Determine Studio Organization
    if payload.studio_id:
        try:
            org_stmt = select(Organization).where(Organization.id == uuid.UUID(payload.studio_id))
        except ValueError:
            org_stmt = select(Organization).where(Organization.slug == payload.studio_id)
    else:
        org_stmt = select(Organization).order_by(Organization.created_at.asc()).limit(1)

    org_res = await db.execute(org_stmt)
    org = org_res.scalar_one_or_none()

    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Studio organization not found.")

    # Check if user is already a member
    mem_stmt = select(OrganizationMember).where(
        OrganizationMember.organization_id == org.id,
        OrganizationMember.user_id == user.id
    )
    existing_mem = (await db.execute(mem_stmt)).scalar_one_or_none()

    if existing_mem:
        existing_mem.is_active = True
        existing_mem.title = payload.title
        existing_mem.bio = payload.bio or existing_mem.bio
        await db.commit()
        await db.refresh(existing_mem)
        return {
            "id": str(existing_mem.id),
            "name": user.full_name,
            "email": user.email,
            "title": existing_mem.title,
            "message": "Teacher already existed; updated details and reactivated."
        }

    new_member = OrganizationMember(
        organization_id=org.id,
        user_id=user.id,
        role="INSTRUCTOR",
        title=payload.title,
        bio=payload.bio or "Certified teacher and guide.",
        is_active=True
    )
    db.add(new_member)
    await db.commit()
    await db.refresh(new_member)

    return {
        "id": str(new_member.id),
        "user_id": str(user.id),
        "name": user.full_name,
        "email": user.email,
        "phone": user.phone,
        "title": new_member.title,
        "bio": new_member.bio,
        "is_active": True,
        "status": "ACTIVE",
        "message": f"Teacher '{user.full_name}' successfully added to {org.name}."
    }

@router.put("/{member_id}")
async def update_studio_teacher(
    member_id: uuid.UUID,
    payload: TeacherUpdateRequest,
    db: AsyncSession = Depends(get_db)
):
    """Modify details or active status for a studio teacher."""
    stmt = (
        select(OrganizationMember, User)
        .join(User, User.id == OrganizationMember.user_id)
        .where(OrganizationMember.id == member_id)
    )
    res = await db.execute(stmt)
    row = res.first()

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Teacher record not found.")

    member, user = row

    if payload.name:
        user.full_name = payload.name.strip()
    if payload.phone:
        user.phone = payload.phone.strip()
    if payload.title is not None:
        member.title = payload.title.strip()
    if payload.bio is not None:
        member.bio = payload.bio.strip()
    if payload.is_active is not None:
        member.is_active = payload.is_active

    await db.commit()
    await db.refresh(member)
    await db.refresh(user)

    return {
        "id": str(member.id),
        "name": user.full_name,
        "email": user.email,
        "phone": user.phone,
        "title": member.title,
        "bio": member.bio,
        "is_active": member.is_active,
        "status": "ACTIVE" if member.is_active else "SUSPENDED",
        "message": f"Teacher '{user.full_name}' updated successfully."
    }

@router.delete("/{member_id}")
async def remove_studio_teacher(
    member_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    """Remove a teacher from the studio."""
    stmt = (
        select(OrganizationMember, User)
        .join(User, User.id == OrganizationMember.user_id)
        .where(OrganizationMember.id == member_id)
    )
    res = await db.execute(stmt)
    row = res.first()

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Teacher record not found.")

    member, user = row
    name = user.full_name

    await db.delete(member)
    await db.commit()

    return {"message": f"Teacher '{name}' successfully removed from the studio."}
