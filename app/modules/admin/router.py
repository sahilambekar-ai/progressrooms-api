from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization
from app.models.user import User
from app.models.commerce import Order
from app.modules.auth.dependencies import require_superadmin

router = APIRouter(prefix="/admin", tags=["Super Admin"])

@router.get("/metrics")
async def get_admin_metrics(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    orgs_count = await db.scalar(select(func.count(Organization.id)))
    users_count = await db.scalar(select(func.count(User.id)))
    orders_sum = await db.scalar(select(func.sum(Order.total_amount)).where(Order.status == "PAID"))

    return {
        "total_organizations": orgs_count or 0,
        "total_users": users_count or 0,
        "total_platform_revenue": float(orders_sum or 0.0),
        "status": "healthy"
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
