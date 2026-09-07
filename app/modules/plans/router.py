from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.plan import Plan

router = APIRouter(prefix="/plans", tags=["Plans & Entitlements"])

@router.get("")
async def list_plans(db: AsyncSession = Depends(get_db)):
    stmt = select(Plan).options(selectinload(Plan.features)).where(Plan.is_public == True)
    res = await db.execute(stmt)
    plans = res.scalars().all()
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "slug": p.slug,
            "description": p.description,
            "price": float(p.price),
            "currency": p.currency,
            "billing_interval": p.billing_interval,
            "status": p.status,
            "features": {f.feature_key: f.feature_value for f in p.features}
        }
        for p in plans
    ]
