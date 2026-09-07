import uuid
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.models.user import User
from app.modules.auth.dependencies import get_current_user
from app.modules.organizations.dependencies import get_current_tenant_member, require_org_role
from app.common.enums import UserRole

router = APIRouter(prefix="/organizations", tags=["Organizations"])

class OrganizationCreate(BaseModel):
    name: str
    slug: str
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"

class OrganizationUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    currency: str | None = None
    logo_url: str | None = None

class MemberInvite(BaseModel):
    email: str
    full_name: str
    role: UserRole = UserRole.INSTRUCTOR
    title: str | None = None
    bio: str | None = None

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # Check slug
    existing = await db.execute(select(Organization).where(Organization.slug == payload.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Slug already taken")

    org = Organization(
        name=payload.name,
        slug=payload.slug,
        timezone=payload.timezone,
        currency=payload.currency
    )
    db.add(org)
    await db.flush()

    # Create Owner membership
    member = OrganizationMember(
        organization_id=org.id,
        user_id=current_user.id,
        role=UserRole.OWNER.value,
        title="Founder / Owner",
        is_active=True
    )
    db.add(member)

    # Initialize settings
    settings = OrganizationSetting(organization_id=org.id)
    db.add(settings)

    await db.commit()
    await db.refresh(org)

    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "timezone": org.timezone,
        "currency": org.currency
    }

@router.get("/{org_id}")
async def get_organization(
    org_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member),
    db: AsyncSession = Depends(get_db)
):
    org, membership = tenant
    stmt = select(Organization).options(
        selectinload(Organization.subscription),
        selectinload(Organization.settings)
    ).where(Organization.id == org_id)
    res = await db.execute(stmt)
    full_org = res.scalar_one()

    return {
        "id": str(full_org.id),
        "name": full_org.name,
        "slug": full_org.slug,
        "timezone": full_org.timezone,
        "currency": full_org.currency,
        "status": full_org.status,
        "role": membership.role if membership else "SUPER_ADMIN",
        "subscription": {
            "status": full_org.subscription.status,
            "starts_at": full_org.subscription.starts_at.isoformat() if full_org.subscription else None,
            "ends_at": full_org.subscription.ends_at.isoformat() if full_org.subscription and full_org.subscription.ends_at else None,
            "trial_ends_at": full_org.subscription.trial_ends_at.isoformat() if full_org.subscription and full_org.subscription.trial_ends_at else None,
        } if full_org.subscription else None
    }

@router.get("/{org_id}/members")
async def list_members(
    org_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(OrganizationMember).options(selectinload(OrganizationMember.user)).where(
        OrganizationMember.organization_id == org_id,
        OrganizationMember.is_active == True
    )
    res = await db.execute(stmt)
    members = res.scalars().all()
    return [
        {
            "id": str(m.id),
            "user_id": str(m.user_id),
            "full_name": m.user.full_name,
            "email": m.user.email,
            "role": m.role,
            "title": m.title,
            "bio": m.bio
        }
        for m in members
    ]
