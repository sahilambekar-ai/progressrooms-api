from fastapi import HTTPException
from sqlalchemy import select, func
from app.models.organization import Organization, OrganizationMember


async def protect_teaching_roster(db, member):
    # Every roster removal locks the studio first, serializing concurrent removals.
    await db.scalar(select(Organization.id).where(Organization.id == member.organization_id).with_for_update())
    if member.role == "STUDIO":
        raise HTTPException(409, "Owner access cannot be removed through teacher management")
    remaining = await db.scalar(select(func.count(OrganizationMember.id)).where(
        OrganizationMember.organization_id == member.organization_id,
        OrganizationMember.can_teach.is_(True), OrganizationMember.is_active.is_(True),
        OrganizationMember.id != member.id))
    if member.can_teach and member.is_active and not remaining:
        raise HTTPException(409, "A studio must retain at least one active teacher")


