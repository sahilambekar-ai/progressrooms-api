import uuid
import math
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember, OrganizationStudent
from app.models.user import User
from app.models.commerce import Order
from app.models.plan import OrganizationSubscription
from app.modules.auth.dependencies import require_superadmin
from app.modules.teachers.service import protect_teaching_roster

router = APIRouter(prefix="/admin", tags=["Super Admin"])

@router.get("/metrics")
async def get_admin_metrics(
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db)
):
    # Independent scalar subqueries avoid join fan-out and share one round trip.
    stmt = select(
        select(func.count(Organization.id)).scalar_subquery(),
        select(func.count(OrganizationMember.id)).scalar_subquery(),
        select(func.count(OrganizationStudent.id)).scalar_subquery(),
        select(func.count(User.id)).scalar_subquery(),
        select(func.sum(Order.total_amount)).where(Order.status == "PAID").scalar_subquery(),
        select(func.count(OrganizationSubscription.id)).where(
            OrganizationSubscription.status == "ACTIVE"
        ).scalar_subquery(),
    )
    orgs_count, instructors_count, students_count, users_count, orders_sum, subs_count = (
        await db.execute(stmt)
    ).one()

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
    stmt = select(Organization).where(Organization.deleted_at.is_(None)).order_by(Organization.created_at.desc())
    res = await db.execute(stmt)
    orgs = res.scalars().all()
    from datetime import datetime, timezone
    from app.models.organization import OrganizationSetting
    from app.models.platform_billing import StudentJoinEvent
    from app.modules.billing.platform import fee, month_at
    # Batch aggregates prevent join fan-out and avoid a query per studio.
    teachers = dict((await db.execute(select(OrganizationMember.organization_id, func.count(OrganizationMember.id))
        .where(OrganizationMember.can_teach.is_(True)).group_by(OrganizationMember.organization_id))).all())
    students = dict((await db.execute(select(OrganizationStudent.organization_id, func.count(OrganizationStudent.id))
        .group_by(OrganizationStudent.organization_id))).all())
    now = datetime.now(timezone.utc)
    months = {o.id: month_at(o, now) for o in orgs}
    active = {(org_id, month): count for org_id, month, count in (await db.execute(
        select(StudentJoinEvent.organization_id, StudentJoinEvent.month, func.count(func.distinct(StudentJoinEvent.user_id)))
        .where(StudentJoinEvent.month.in_(set(months.values())))
        .group_by(StudentJoinEvent.organization_id, StudentJoinEvent.month))).all()} if orgs else {}
    settings = {row.organization_id: row for row in (await db.scalars(select(OrganizationSetting))).all()}
    from sqlalchemy import or_
    pricing_review = set((await db.execute(select(StudentJoinEvent.organization_id, StudentJoinEvent.month)
        .where(StudentJoinEvent.month.in_(set(months.values())), or_(StudentJoinEvent.pricing_region.is_(None),
            StudentJoinEvent.pricing_region != "INDIA")).distinct())).all()) if orgs else set()
    owners = {}
    for org_id, name, email in (await db.execute(select(OrganizationMember.organization_id, User.full_name, User.email)
        .join(User, User.id == OrganizationMember.user_id).where(OrganizationMember.role == "STUDIO")
        .order_by(OrganizationMember.created_at))).all():
        owners.setdefault(org_id, {"name": name, "email": email})
    return [
        {
            "id": str(o.id),
            "name": o.name,
            "slug": o.slug,
            "status": o.status,
            "created_at": o.created_at.isoformat(),
            "owner": owners.get(o.id),
            "phone": (settings[o.id].phone or settings[o.id].support_phone) if o.id in settings else None,
            "city": settings[o.id].city if o.id in settings else None,
            "state": settings[o.id].state if o.id in settings else None,
            "teaching_mode": settings[o.id].teaching_mode if o.id in settings else None,
            "account_completed": bool(settings[o.id].account_completed) if o.id in settings else False,
            "total_teachers": teachers.get(o.id, 0),
            "total_students": students.get(o.id, 0),
            "active_students": active.get((o.id, months[o.id]), 0),
            "estimated_platform_fee": None if (o.id, months[o.id]) in pricing_review else fee(active.get((o.id, months[o.id]), 0)),
            "billing_currency": "INR",
            "billing_month": months[o.id].isoformat(),
            "timezone": o.timezone,
        }
        for o in orgs
    ]


@router.get("/organizations/{org_id}")
async def studio_details(org_id: uuid.UUID, admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timezone
    from app.models.organization import OrganizationSetting
    from app.models.platform_billing import StudentJoinEvent
    from app.modules.billing.platform import month_at
    org = await db.get(Organization, org_id)
    if not org or org.deleted_at:
        raise HTTPException(404, "Studio not found")
    settings = await db.scalar(select(OrganizationSetting).where(OrganizationSetting.organization_id == org_id))
    fields = ("phone", "whatsapp_number", "support_email", "support_phone", "studio_tagline", "disciplines", "teaching_mode",
        "address_line1", "address_line2", "city", "state", "pincode", "country", "has_gst", "gst_number",
        "legal_business_name", "pan_number", "bank_name", "account_holder_name", "ifsc_code", "upi_id", "settlement_cycle",
        "account_completed", "completion_percentage", "brand_color", "zoom_auto_meeting_enabled", "zoom_waiting_room",
        "zoom_host_video", "zoom_enforce_basic_login", "zoom_enforce_zoom_login", "zoom_group_allow_self_buy_subscriptions",
        "zoom_group_active_subscription_required", "zoom_group_confirm_checkin_everytime", "zoom_personal_allow_self_buy_subscriptions",
        "zoom_personal_active_subscription_required", "zoom_personal_confirm_checkin_everytime", "zoom_personal_no_show_penalty",
        "zoom_join_before_host", "zoom_cloud_recording_mode")
    profile = {field: getattr(settings, field) if settings else None for field in fields}
    # This endpoint is super-admin-only. Never return Zoom keys or payment credentials.
    profile["account_number"] = settings.account_number_enc if settings else None
    owners = (await db.execute(select(User.full_name, User.email).join(OrganizationMember,
        OrganizationMember.user_id == User.id).where(OrganizationMember.organization_id == org_id,
        OrganizationMember.role == "STUDIO"))).all()
    from urllib.parse import quote
    from app.models.website import OrganizationWebsite
    from app.modules.websites import rendering
    website = await db.scalar(select(OrganizationWebsite).where(OrganizationWebsite.organization_id == org_id))
    release = next((entry["website_release"] for entry in (website.custom_sections or [])
        if isinstance(entry, dict) and "website_release" in entry), None) if website else None
    published = False
    if org.status == "ACTIVE" and website and website.is_published and isinstance(release, dict) and release.get("version"):
        try:
            published = rendering.version_path(str(org.id), release["version"]).exists()
        except HTTPException:
            # A missing published artifact must not prevent admins viewing the profile.
            published = False
    return dict(id=str(org.id), name=org.name, slug=org.slug, status=org.status, timezone=org.timezone,
        website_url=f"/{quote(org.slug, safe='')}" if published else None,
        currency=org.currency, created_at=org.created_at.isoformat(), profile=profile,
        contacts=[dict(name=name, email=email) for name, email in owners],
        total_teachers=await db.scalar(select(func.count(OrganizationMember.id)).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.can_teach.is_(True))) or 0,
        total_students=await db.scalar(select(func.count(OrganizationStudent.id)).where(OrganizationStudent.organization_id == org_id)) or 0,
        active_students=await db.scalar(select(func.count(func.distinct(StudentJoinEvent.user_id))).where(
            StudentJoinEvent.organization_id == org_id, StudentJoinEvent.month == month_at(org, datetime.now(timezone.utc)))) or 0)


@router.get("/organizations/{org_id}/progressrooms")
async def studio_billing_dashboard(org_id: uuid.UUID, month: date | None = None,
    teacher_id: uuid.UUID | None = None, admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    from app.modules.billing.platform import dashboard
    org = await db.get(Organization, org_id)
    if not org or org.deleted_at:
        raise HTTPException(404, "Studio not found")
    result = await dashboard(db, org, month, teacher_id)
    await db.commit()
    return result


@router.get("/organizations/{org_id}/teachers")
async def studio_teacher_filters(org_id: uuid.UUID, admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(OrganizationMember.id, User.full_name).join(User, User.id == OrganizationMember.user_id)
        .where(OrganizationMember.organization_id == org_id, OrganizationMember.can_teach.is_(True)).order_by(User.full_name))).all()
    return [dict(id=str(member_id), name=name) for member_id, name in rows]

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
            "bio": member.bio or "Instructor.",
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

    if member.is_active:
        await protect_teaching_roster(db, member)
        from app.modules.zoom.service import disconnect
        await disconnect(db, member, pause_confirmed=True, commit=False)
    member.is_active = not member.is_active
    await db.commit()
    await db.refresh(member)

    return {
        "id": str(member.id),
        "is_active": member.is_active,
        "status": "ACTIVE" if member.is_active else "SUSPENDED",
        "message": f"Instructor status updated to {'ACTIVE' if member.is_active else 'SUSPENDED'}"
    }


@router.get("/instructors/{member_id}/zoom-impact")
async def instructor_zoom_impact(member_id: uuid.UUID, admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    member = await db.get(OrganizationMember, member_id)
    if not member:
        raise HTTPException(404, "Instructor not found")
    from app.modules.zoom.service import impact
    return await impact(db, member.organization_id, member.id)
