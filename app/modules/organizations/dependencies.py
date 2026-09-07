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

    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    if current_user.is_superadmin:
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

    return org, membership

def require_org_role(allowed_roles: list[UserRole]):
    async def dependency(
        tenant_context: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member)
    ) -> tuple[Organization, OrganizationMember | None]:
        org, membership = tenant_context
        if membership is None:
            # Superadmin bypass
            return org, membership

        if UserRole(membership.role) not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied. Required role: {[r.value for r in allowed_roles]}"
            )
        return org, membership
    return dependency
