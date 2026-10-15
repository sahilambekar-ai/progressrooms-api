import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember
from app.models.session import Session
from app.models.membership_tier import MembershipTier, MembershipTierClass
from app.modules.organizations.dependencies import get_current_tenant_member, require_org_role
from app.modules.membership_tiers.schemas import MembershipTierCreate, MembershipTierUpdate, MembershipTierToggleStatus
from app.common.enums import UserRole

router = APIRouter(prefix="/organizations/{org_id}/membership-tiers", tags=["Memberships & Passes"])

async def get_tier_dict(db: AsyncSession, tier: MembershipTier) -> dict:
    c_stmt = (
        select(Session.id, Session.name, Session.is_active)
        .join(MembershipTierClass, MembershipTierClass.session_id == Session.id)
        .where(MembershipTierClass.tier_id == tier.id, Session.deleted_at.is_(None))
    )
    c_res = await db.execute(c_stmt)
    attached = [
        {"id": str(s_id), "name": s_name, "is_active": s_active}
        for s_id, s_name, s_active in c_res.all()
    ]
    return {
        "id": str(tier.id),
        "organization_id": str(tier.organization_id),
        "name": tier.name,
        "slug": tier.slug,
        "tier_type": tier.tier_type,
        "price": float(tier.price),
        "offer_price": float(tier.offer_price) if tier.offer_price is not None else None,
        "currency": tier.currency,
        "validity_days": tier.validity_days,
        "total_classes": tier.total_classes,
        "description": tier.description,
        "is_active": tier.is_active,
        "created_at": tier.created_at.isoformat() if tier.created_at else None,
        "attached_classes": attached,
        "attached_class_ids": [c["id"] for c in attached]
    }

@router.get("")
async def list_membership_tiers(
    org_id: uuid.UUID,
    history: bool = False,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member),
    db: AsyncSession = Depends(get_db)
):
    stmt = (
        select(MembershipTier)
        .where(MembershipTier.organization_id == org_id, MembershipTier.deleted_at.is_not(None) if history else MembershipTier.deleted_at.is_(None))
        .order_by(MembershipTier.deleted_at.desc() if history else MembershipTier.created_at.desc())
    )
    res = await db.execute(stmt)
    tiers = res.scalars().all()

    tier_ids = [t.id for t in tiers]
    classes_by_tier: dict[uuid.UUID, list[dict]] = {tid: [] for tid in tier_ids}
    if tier_ids:
        c_stmt = (
            select(MembershipTierClass.tier_id, Session.id, Session.name, Session.is_active)
            .join(Session, Session.id == MembershipTierClass.session_id)
            .where(MembershipTierClass.tier_id.in_(tier_ids))
        )
        if not history:
            c_stmt = c_stmt.where(Session.deleted_at.is_(None))
        c_res = await db.execute(c_stmt)
        for t_id, s_id, s_name, s_active in c_res.all():
            classes_by_tier[t_id].append({
                "id": str(s_id),
                "name": s_name,
                "is_active": s_active
            })

    result = []
    for t in tiers:
        attached = classes_by_tier.get(t.id, [])
        result.append({
            "id": str(t.id),
            "organization_id": str(t.organization_id),
            "name": t.name,
            "slug": t.slug,
            "tier_type": t.tier_type,
            "price": float(t.price),
            "offer_price": float(t.offer_price) if t.offer_price is not None else None,
            "currency": t.currency,
            "validity_days": t.validity_days,
            "total_classes": t.total_classes,
            "description": t.description,
            "is_active": t.is_active,
            "deleted_at": t.deleted_at.isoformat() if t.deleted_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "attached_classes": attached,
            "attached_class_ids": [c["id"] for c in attached]
        })
    return result

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_membership_tier(
    org_id: uuid.UUID,
    payload: MembershipTierCreate,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db)
):
    slug = f"{payload.name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}"
    
    if payload.class_ids:
        stmt = select(Session.id).where(Session.id.in_(payload.class_ids), Session.organization_id == org_id, Session.deleted_at.is_(None))
        valid_ids = (await db.execute(stmt)).scalars().all()
        if len(valid_ids) != len(set(payload.class_ids)):
            raise HTTPException(422, "One or more classes do not belong to this studio")

    tier = MembershipTier(
        organization_id=org_id,
        name=payload.name,
        slug=slug,
        tier_type=payload.tier_type,
        price=payload.price,
        offer_price=payload.offer_price,
        currency=payload.currency,
        validity_days=payload.validity_days,
        total_classes=payload.total_classes,
        description=payload.description,
        is_active=payload.is_active
    )
    db.add(tier)
    await db.flush()

    for cid in set(payload.class_ids):
        db.add(MembershipTierClass(tier_id=tier.id, session_id=cid))

    await db.commit()
    return await get_tier_dict(db, tier)

@router.put("/{tier_id}")
async def update_membership_tier(
    org_id: uuid.UUID,
    tier_id: uuid.UUID,
    payload: MembershipTierUpdate,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(MembershipTier).where(MembershipTier.id == tier_id, MembershipTier.organization_id == org_id, MembershipTier.deleted_at.is_(None))
    res = await db.execute(stmt)
    tier = res.scalar_one_or_none()
    if not tier:
        raise HTTPException(404, "Membership tier not found")

    # Validate the resulting prices, including edits that supply only one field.
    new_price = payload.price if payload.price is not None else tier.price
    new_offer = payload.offer_price if "offer_price" in payload.model_fields_set else tier.offer_price
    if new_offer is not None and new_offer > new_price:
        raise HTTPException(422, "Offer price must not be greater than price")
    if "offer_price" in payload.model_fields_set:
        tier.offer_price = payload.offer_price

    if payload.name is not None:
        tier.name = payload.name
    if payload.tier_type is not None:
        tier.tier_type = payload.tier_type
    if payload.price is not None:
        tier.price = payload.price
    if payload.currency is not None:
        tier.currency = payload.currency
    if payload.validity_days is not None:
        tier.validity_days = payload.validity_days
    if payload.total_classes is not None:
        tier.total_classes = payload.total_classes
    if payload.description is not None:
        tier.description = payload.description
    if payload.is_active is not None:
        tier.is_active = payload.is_active

    if payload.class_ids is not None:
        if payload.class_ids:
            check_stmt = select(Session.id).where(Session.id.in_(payload.class_ids), Session.organization_id == org_id, Session.deleted_at.is_(None))
            valid_ids = (await db.execute(check_stmt)).scalars().all()
            if len(valid_ids) != len(set(payload.class_ids)):
                raise HTTPException(422, "One or more classes do not belong to this studio")

        del_stmt = delete(MembershipTierClass).where(MembershipTierClass.tier_id == tier.id)
        await db.execute(del_stmt)

        for cid in set(payload.class_ids):
            db.add(MembershipTierClass(tier_id=tier.id, session_id=cid))

    await db.commit()
    return await get_tier_dict(db, tier)

@router.patch("/{tier_id}/toggle-status")
async def toggle_membership_tier_status(
    org_id: uuid.UUID,
    tier_id: uuid.UUID,
    payload: MembershipTierToggleStatus | None = None,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(MembershipTier).where(MembershipTier.id == tier_id, MembershipTier.organization_id == org_id, MembershipTier.deleted_at.is_(None))
    res = await db.execute(stmt)
    tier = res.scalar_one_or_none()
    if not tier:
        raise HTTPException(404, "Membership tier not found")

    if payload and payload.is_active is not None:
        tier.is_active = payload.is_active
    else:
        tier.is_active = not tier.is_active

    await db.commit()
    return {"id": str(tier.id), "is_active": tier.is_active}

@router.delete("/{tier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_membership_tier(
    org_id: uuid.UUID,
    tier_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(MembershipTier).where(MembershipTier.id == tier_id, MembershipTier.organization_id == org_id, MembershipTier.deleted_at.is_(None))
    res = await db.execute(stmt)
    tier = res.scalar_one_or_none()
    if not tier:
        raise HTTPException(404, "Membership tier not found")

    tier.deleted_at = datetime.now(timezone.utc)
    await db.commit()
