import uuid
from fastapi import Depends, HTTPException, status, Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.modules.auth.dependencies import get_current_user
from app.models.user import User
from app.models.organization import Organization, OrganizationMember
from app.common.enums import UserRole

async def get_current_tenant_member(
    org_id: uuid.UUID = Path(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> tuple[Organization, OrganizationMember | None]:
    """Validates tenant exists and resolves current user's membership within this organization."""
    org_stmt = select(Organization).where(Organization.id == org_id, Organization.deleted_at.is_(None))
    org_res = await db.execute(org_stmt)
    org = org_res.scalar_one_or_none()

    if not org or org.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    if current_user.role == "SUPER_ADMIN":
        # Superadmin has full bypass access
        return org, None

    mem_stmt = select(OrganizationMember).where(
        OrganizationMember.organization_id == org_id,
        OrganizationMember.user_id == current_user.id,
        OrganizationMember.is_active == True
    )
    mem_res = await db.execute(mem_stmt)
    membership = mem_res.scalar_one_or_none()

    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this organization"
        )

    if membership.role != current_user.role:
        raise HTTPException(403, "Account and studio membership roles do not match")
    return org, membership

def require_org_role(allowed_roles: list[UserRole]):
    async def dependency(
        tenant_context: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member)
    ) -> tuple[Organization, OrganizationMember | None]:
        org, membership = tenant_context
        if membership is None:
            # Superadmin bypass
            return org, membership

        if membership.role not in [r.value for r in allowed_roles]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied. Required role: {[r.value for r in allowed_roles]}"
            )
        return org, membership
    return dependency

async def resolve_staff_studio(db, user, studio_id=None, owner_only=False):
    """Resolve an authorized studio without ever defaulting to another tenant."""
    if user.role not in ({"STUDIO"} if owner_only else {"STUDIO", "TEACHER"}):
        raise HTTPException(403, "Studio owner access required" if owner_only else "Studio staff access required")
    stmt = select(Organization, OrganizationMember).join(OrganizationMember).where(
        OrganizationMember.user_id == user.id, OrganizationMember.is_active == True,
        OrganizationMember.role == user.role, Organization.deleted_at.is_(None), Organization.status == "ACTIVE"
    ).order_by(OrganizationMember.created_at.asc())
    if studio_id:
        try:
            stmt = stmt.where(Organization.id == uuid.UUID(str(studio_id)))
        except ValueError:
            stmt = stmt.where(Organization.slug == studio_id)
    rows = (await db.execute(stmt)).all()
    if len(rows) == 0:
        raise HTTPException(403, "Select a studio you belong to")
    return rows[0]

