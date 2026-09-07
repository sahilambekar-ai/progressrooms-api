import uuid
import math
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember, OrganizationStudent
from app.models.user import User
from app.models.commerce import Order
from app.models.plan import OrganizationSubscription
from app.modules.auth.dependencies import require_superadmin

router = APIRouter(prefix="/admin", tags=["Super Admin"])

@router.get("/metrics")
async def get_admin_metrics(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    orgs_count = await db.scalar(select(func.count(Organization.id)))
    instructors_count = await db.scalar(select(func.count(OrganizationMember.id)))
    students_count = await db.scalar(select(func.count(OrganizationStudent.id)))
    users_count = await db.scalar(select(func.count(User.id)))
    orders_sum = await db.scalar(select(func.sum(Order.total_amount)).where(Order.status == "PAID"))
    subs_count = await db.scalar(select(func.count(OrganizationSubscription.id)).where(OrganizationSubscription.status == "ACTIVE"))

    return {
        "total_organizations": orgs_count or 0,
        "total_instructors": instructors_count or 0,
        "total_students": students_count or 0,
        "total_users": users_count or 0,
        "total_platform_revenue": float(orders_sum or 0.0),
        "active_subscriptions": subs_count or 0,
        "status": "healthy",
        "system_status": "All Systems Operational"
    }

@router.get("/organizations")
async def list_all_organizations(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Organization).order_by(Organization.created_at.desc())
    res = await db.execute(stmt)
    orgs = res.scalars().all()
    return [
        {
            "id": str(o.id),
            "name": o.name,
            "slug": o.slug,
            "status": o.status,
            "created_at": o.created_at.isoformat()
        }
        for o in orgs
    ]

@router.get("/instructors")
async def list_all_instructors(
    page: int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=50),
    search: str | None = Query(None),
    status: str | None = Query(None),
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """List all instructors across all studios with search, filter, and pagination for Super Admin."""
    stmt = (
        select(OrganizationMember, User, Organization)
        .join(User, User.id == OrganizationMember.user_id)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
    )

    # Search filter
    if search:
        search_term = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(User.full_name).like(search_term),
                func.lower(User.email).like(search_term),
                func.lower(Organization.name).like(search_term),
                func.lower(OrganizationMember.title).like(search_term)
            )
        )

    # Status filter
    if status and status != "ALL":
        if status == "ACTIVE":
            stmt = stmt.where(OrganizationMember.is_active == True)
        elif status == "SUSPENDED":
            stmt = stmt.where(OrganizationMember.is_active == False)

    # Total Count query
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_items = (await db.scalar(count_stmt)) or 0
    total_pages = max(1, math.ceil(total_items / page_size))

    # Apply pagination
    offset = (page - 1) * page_size
    stmt = stmt.order_by(Organization.name.asc(), OrganizationMember.created_at.desc()).offset(offset).limit(page_size)

    result = await db.execute(stmt)
    rows = result.all()

    items = []
    for member, user, org in rows:
        items.append({
            "id": str(member.id),
            "user_id": str(user.id),
            "instructor_name": user.full_name,
            "email": user.email,
            "phone": user.phone or "+91 98765 00000",
            "studio_id": str(org.id),
            "studio_name": org.name,
            "studio_slug": org.slug,
            "role": member.role,
            "title": member.title or ("Lead Teacher" if member.role == "OWNER" else "Instructor"),
            "bio": member.bio or "Certified somatic movement and yoga instructor.",
            "is_active": member.is_active,
            "status": "ACTIVE" if member.is_active else "SUSPENDED",
            "joined_at": member.created_at.strftime("%b %d, %Y") if member.created_at else "Recently"
        })

    return {
        "items": items,
        "total_items": total_items,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }

@router.post("/instructors/{member_id}/toggle-status")
async def toggle_instructor_status(
    member_id: uuid.UUID,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    """Toggle an instructor's status between Active and Suspended."""
    stmt = select(OrganizationMember).where(OrganizationMember.id == member_id)
    res = await db.execute(stmt)
    member = res.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instructor not found")

    member.is_active = not member.is_active
    await db.commit()
    await db.refresh(member)

    return {
        "id": str(member.id),
        "is_active": member.is_active,
        "status": "ACTIVE" if member.is_active else "SUSPENDED",
        "message": f"Instructor status updated to {'ACTIVE' if member.is_active else 'SUSPENDED'}"
    }
