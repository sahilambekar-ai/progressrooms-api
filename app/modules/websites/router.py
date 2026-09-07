import uuid
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.models.website import OrganizationWebsite, WebsiteTemplate
from app.models.session import Session, ClassType, SessionPricing, SessionRequirement, SessionInstructor
from app.models.schedule import SessionScheduleRule
from app.modules.organizations.dependencies import require_org_role
from app.common.enums import UserRole

router = APIRouter(tags=["Websites & Public Platform"])

class WebsiteUpdatePayload(BaseModel):
    template_code: str = "DEFAULT"
    headline: str | None = None
    subheadline: str | None = None
    hero_image_url: str | None = None
    about_text: str | None = None
    primary_color: str = "#4f46e5"
    accent_color: str = "#06b6d4"
    social_links: dict | None = None

@router.get("/public/organizations/{org_slug}")
async def get_public_organization_website(org_slug: str, db: AsyncSession = Depends(get_db)):
    """Server-side rendered public teacher / studio landing payload."""
    stmt = select(Organization).options(
        selectinload(Organization.website).selectinload(OrganizationWebsite.template),
        selectinload(Organization.settings),
        selectinload(Organization.members).selectinload(OrganizationMember.user),
        selectinload(Organization.class_types),
        selectinload(Organization.sessions).selectinload(Session.pricing),
        selectinload(Organization.sessions).selectinload(Session.schedule_rules)
    ).where(
        Organization.slug == org_slug,
        Organization.status == "ACTIVE",
        Organization.deleted_at.is_(None)
    )
    res = await db.execute(stmt)
    org = res.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Teacher or Studio not found")

    public_sessions = [
        {
            "id": str(s.id),
            "name": s.name,
            "slug": s.slug,
            "short_description": s.short_description,
            "session_type": s.session_type,
            "skill_level": s.skill_level,
            "duration_minutes": s.duration_minutes,
            "capacity": s.capacity,
            "pricing": {
                "pricing_type": s.pricing.pricing_type,
                "price": float(s.pricing.price),
                "currency": s.pricing.currency
            } if s.pricing else None,
            "schedule": [
                {
                    "day_of_week": r.day_of_week,
                    "start_time": r.start_time.isoformat(),
                    "end_time": r.end_time.isoformat()
                }
                for r in s.schedule_rules if r.is_active
            ]
        }
        for s in org.sessions if s.is_public and s.is_active and s.deleted_at is None
    ]

    return {
        "organization": {
            "id": str(org.id),
            "name": org.name,
            "slug": org.slug,
            "logo_url": org.logo_url,
            "timezone": org.timezone,
            "currency": org.currency
        },
        "website": {
            "template_code": org.website.template.code if org.website and org.website.template else "DEFAULT",
            "headline": org.website.headline if org.website else f"Welcome to {org.name}",
            "subheadline": org.website.subheadline if org.website else "Book scheduled classes, workshops, and coaching.",
            "hero_image_url": org.website.hero_image_url if org.website else None,
            "about_text": org.website.about_text if org.website else None,
            "primary_color": org.website.primary_color if org.website else "#4f46e5",
            "accent_color": org.website.accent_color if org.website else "#06b6d4",
            "social_links": org.website.social_links if org.website else {}
        },
        "instructors": [
            {
                "name": m.user.full_name,
                "role": m.role,
                "title": m.title,
                "bio": m.bio,
                "avatar_url": m.user.avatar_url
            }
            for m in org.members if m.is_active
        ],
        "class_types": [
            {"id": str(ct.id), "name": ct.name, "slug": ct.slug}
            for ct in org.class_types if ct.deleted_at is None
        ],
        "sessions": public_sessions
    }

@router.get("/public/organizations/{org_slug}/sessions/{session_slug}")
async def get_public_session_detail(org_slug: str, session_slug: str, db: AsyncSession = Depends(get_db)):
    """Dedicated SSR Session detail page payload."""
    stmt = select(Session).join(Session.organization).options(
        selectinload(Session.organization),
        selectinload(Session.class_type),
        selectinload(Session.pricing),
        selectinload(Session.requirements),
        selectinload(Session.schedule_rules),
        selectinload(Session.instructors).selectinload(SessionInstructor.member).selectinload(OrganizationMember.user)
    ).where(
        Organization.slug == org_slug,
        Session.slug == session_slug,
        Session.is_public == True,
        Session.is_active == True,
        Session.deleted_at.is_(None)
    )
    res = await db.execute(stmt)
    session = res.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    return {
        "id": str(session.id),
        "name": session.name,
        "slug": session.slug,
        "short_description": session.short_description,
        "full_description": session.full_description,
        "session_type": session.session_type,
        "capacity": session.capacity,
        "min_age": session.min_age,
        "max_age": session.max_age,
        "skill_level": session.skill_level,
        "duration_minutes": session.duration_minutes,
        "language": session.language,
        "organization": {
            "id": str(session.organization.id),
            "name": session.organization.name,
            "slug": session.organization.slug,
            "timezone": session.organization.timezone,
            "currency": session.organization.currency
        },
        "class_type": session.class_type.name if session.class_type else None,
        "pricing": {
            "pricing_type": session.pricing.pricing_type,
            "price": float(session.pricing.price),
            "currency": session.pricing.currency,
            "total_classes": session.pricing.total_classes,
            "validity_days": session.pricing.validity_days
        } if session.pricing else None,
        "requirements": {
            "prerequisites": session.requirements.prerequisites if session.requirements else None,
            "required_equipment": session.requirements.required_equipment if session.requirements else None,
            "preparation_instructions": session.requirements.preparation_instructions if session.requirements else None,
            "additional_notes": session.requirements.additional_notes if session.requirements else None
        } if session.requirements else None,
        "instructors": [
            {
                "name": i.member.user.full_name,
                "role": i.role,
                "bio": i.member.bio,
                "title": i.member.title
            }
            for i in session.instructors
        ],
        "schedule": [
            {
                "day_of_week": r.day_of_week,
                "start_time": r.start_time.isoformat(),
                "end_time": r.end_time.isoformat()
            }
            for r in session.schedule_rules if r.is_active
        ]
    }

@router.put("/organizations/{org_id}/website")
async def update_organization_website(
    org_id: uuid.UUID,
    payload: WebsiteUpdatePayload,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN])),
    db: AsyncSession = Depends(get_db)
):
    tmpl_stmt = select(WebsiteTemplate).where(WebsiteTemplate.code == payload.template_code)
    tmpl_res = await db.execute(tmpl_stmt)
    tmpl = tmpl_res.scalar_one_or_none()
    if not tmpl:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid template code")

    ws_stmt = select(OrganizationWebsite).where(OrganizationWebsite.organization_id == org_id)
    ws_res = await db.execute(ws_stmt)
    ws = ws_res.scalar_one_or_none()

    if not ws:
        ws = OrganizationWebsite(
            organization_id=org_id,
            template_id=tmpl.id,
            headline=payload.headline,
            subheadline=payload.subheadline,
            hero_image_url=payload.hero_image_url,
            about_text=payload.about_text,
            primary_color=payload.primary_color,
            accent_color=payload.accent_color,
            social_links=payload.social_links or {}
        )
        db.add(ws)
    else:
        ws.template_id = tmpl.id
        ws.headline = payload.headline
        ws.subheadline = payload.subheadline
        ws.hero_image_url = payload.hero_image_url
        ws.about_text = payload.about_text
        ws.primary_color = payload.primary_color
        ws.accent_color = payload.accent_color
        if payload.social_links:
            ws.social_links = payload.social_links

    await db.commit()
    return {"message": "Website configuration updated successfully"}
