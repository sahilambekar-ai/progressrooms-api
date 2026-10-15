import uuid
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile
from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.models.website import OrganizationWebsite, WebsiteTemplate
from app.models.session import Session, SessionInstructor
from app.models.schedule import ClassOccurrence
from app.models.commerce import SessionEnrollment
from app.models.membership_tier import MembershipTier, MembershipTierClass
from app.modules.organizations.dependencies import require_org_role
from app.common.enums import UserRole
from app.modules.auth.dependencies import get_current_user

router = APIRouter(tags=["Websites & Public Platform"])

from fastapi.responses import HTMLResponse, FileResponse
from app.core.config import settings
from .schemas import WebsiteUpdatePayload, TEMPLATES, PRESET_NAMES
from .catalog import catalog, DESIGNS, sample_content
from . import rendering

EDITOR_ROLES = [UserRole.STUDIO]
HTML_HEADERS = {"Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}


def get_entry(ws, key, default=None):
    return next((item[key] for item in (ws.custom_sections or []) if isinstance(item, dict) and key in item), default) if ws else default


def set_entry(ws, key, value):
    ws.custom_sections = [item for item in (ws.custom_sections or []) if not isinstance(item, dict) or key not in item] + [{key: value}]


def website_content(ws):
    published = get_entry(ws, "website_published")
    if published:
        return WebsiteUpdatePayload.model_validate(published).model_dump()
    if not ws:
        return WebsiteUpdatePayload.model_validate(sample_content(DESIGNS[0])).model_dump()
    return WebsiteUpdatePayload.model_validate({
        "template_code": ws.template.code if ws.template else "TEMPLATE_1",
        "headline": ws.headline or "A little space for something extraordinary.",
        "subheadline": ws.subheadline or "", "hero_image_url": ws.hero_image_url or "",
        "about_text": ws.about_text or "", "primary_color": ws.primary_color,
        "accent_color": ws.accent_color,
    }).model_dump()


@router.get("/public/website-templates")
async def website_templates():
    return catalog()


@router.get("/public/website-templates/{code}/preview", response_class=HTMLResponse)
async def template_preview(code: str):
    design = next((item for item in DESIGNS if item["code"] == code), None)
    if not design:
        raise HTTPException(404, "Template not found")
    content = WebsiteUpdatePayload.model_validate(sample_content(design))
    html = await rendering.render_document(content, "Your Studio", "your-studio", settings.WEBSITE_PUBLIC_ORIGIN)
    return HTMLResponse(html, headers=HTML_HEADERS)


@router.get("/public/website-assets/{filename}")
async def template_asset(filename: str):
    if filename not in {f"{name}.jpg" for name in PRESET_NAMES}:
        raise HTTPException(404, "Image not found")
    return FileResponse(rendering.ASSET_DIR / filename, headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


async def load_website(db, org_id):
    return (await db.execute(select(OrganizationWebsite).options(selectinload(OrganizationWebsite.template)).where(OrganizationWebsite.organization_id == org_id))).scalar_one_or_none()

@router.get("/website-workspace")
async def website_workspace(user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Organization).join(OrganizationMember).where(
        OrganizationMember.user_id == user.id, OrganizationMember.is_active == True,
        OrganizationMember.role.in_([role.value for role in EDITOR_ROLES]),
        Organization.deleted_at.is_(None)
    ).order_by(Organization.name))).scalars().all()
    return [{"id": str(org.id), "name": org.name, "slug": org.slug} for org in rows]

@router.get("/organizations/{org_id}/website")
async def get_website_editor(org_id: uuid.UUID, tenant=Depends(require_org_role(EDITOR_ROLES)), db: AsyncSession = Depends(get_db)):
    ws = await load_website(db, org_id)
    draft = get_entry(ws, "website_draft")
    org, _ = tenant
    editor_content = WebsiteUpdatePayload.model_validate(draft).model_dump() if draft else website_content(ws)
    # Upgrade the old fixed-page editor with editable starter sections. A deliberate
    # empty sections/images array in the new format is always preserved.
    if (draft is not None and "sections" not in draft) or (draft is None and not get_entry(ws, "website_published")):
        design = next(item for item in DESIGNS if item["code"] == editor_content["template_code"])
        starter = sample_content(design)
        if not editor_content["images"]:
            editor_content["images"] = starter["images"]
        starter["sections"][0]["cards"] = [dict(card, image_id=editor_content["images"][i % len(editor_content["images"])]["id"]) for i, card in enumerate(starter["sections"][0]["cards"])]
        editor_content["sections"] = starter["sections"]
    release = get_entry(ws, "website_release")
    has_artifact = False
    if release and isinstance(release, dict) and release.get("version"):
        artifact_file = rendering.version_path(str(org.id), release["version"])
        has_artifact = artifact_file.exists()

    if not ws or not ws.is_published or not has_artifact:
        website_state = "Draft"
    else:
        published_content = get_entry(ws, "website_published")
        draft_content = get_entry(ws, "website_draft")
        if draft_content and published_content and draft_content != published_content:
            website_state = "Published with unpublished changes"
        else:
            website_state = "Published"

    return {
        "organization": {"id": str(org.id), "name": org.name, "slug": org.slug},
        "content": editor_content,
        "is_published": bool(ws and ws.is_published and has_artifact),
        "website_state": website_state,
        "has_published_artifact": has_artifact,
        "release": release if has_artifact else None,
        "versions": get_entry(ws, "website_versions", []),
    }

@router.get("/public/organizations/{org_slug}")
async def get_public_organization_website(org_slug: str, db: AsyncSession = Depends(get_db)):
    """Server-side rendered public teacher / studio landing payload."""
    stmt = select(Organization).options(
        selectinload(Organization.website).selectinload(OrganizationWebsite.template),
        selectinload(Organization.members).selectinload(OrganizationMember.user),
        selectinload(Organization.class_types),
        selectinload(Organization.sessions).selectinload(Session.pricing),
        selectinload(Organization.sessions).selectinload(Session.schedule_rules),
        selectinload(Organization.sessions).selectinload(Session.instructors).selectinload(SessionInstructor.member).selectinload(OrganizationMember.user)
    ).where(
        Organization.slug == org_slug,
        Organization.status == "ACTIVE",
        Organization.deleted_at.is_(None)
    )
    res = await db.execute(stmt)
    org = res.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Teacher or Studio not found")

    from app.models.commerce import SessionEnrollment
    active_public_sessions = [s for s in org.sessions if s.is_public and s.is_active and s.deleted_at is None]
    public_sessions = []
    for s in active_public_sessions:
        enrolled_count = await db.scalar(
            select(func.count(SessionEnrollment.id)).where(
                SessionEnrollment.session_id == s.id,
                SessionEnrollment.status == "ACTIVE"
            )
        ) or 0
        remaining_spots = max(0, s.capacity - enrolled_count)
        primary_instructor = s.instructors[0].member.user.full_name if s.instructors and s.instructors[0].member and s.instructors[0].member.user else None
        public_sessions.append({
            "id": str(s.id),
            "name": s.name,
            "slug": s.slug,
            "short_description": s.short_description,
            "session_type": s.session_type,
            "delivery_mode": "ONLINE_LIVE" if s.session_type in ("ONLINE", "LIVE") else "HYBRID",
            "skill_level": s.skill_level,
            "duration_minutes": s.duration_minutes,
            "capacity": s.capacity,
            "remaining_spots": remaining_spots,
            "instructor_name": primary_instructor,
            "pricing": {
                "pricing_type": s.pricing.pricing_type,
                "price": float(s.pricing.price),
                "currency": s.pricing.currency,
                "total_classes": s.pricing.total_classes if s.pricing.total_classes is not None else (1 if s.pricing.pricing_type in ("ONE_TIME", "PER_SESSION") else None),
                "validity_days": s.pricing.validity_days
            } if s.pricing else None,
            "schedule": [
                {
                    "day_of_week": r.day_of_week,
                    "start_time": r.start_time.isoformat(),
                    "end_time": r.end_time.isoformat()
                }
                for r in s.schedule_rules if r.is_active
            ]
        })

    tiers = (await db.execute(select(MembershipTier).options(
        selectinload(MembershipTier.classes).selectinload(MembershipTierClass.session)
    ).where(MembershipTier.organization_id == org.id, MembershipTier.is_active == True,
            MembershipTier.deleted_at.is_(None)).order_by(MembershipTier.name))).scalars().all()
    passes = [{"id": str(t.id), "name": t.name, "description": t.description,
               "price": float(t.price), "offer_price": float(t.offer_price) if t.offer_price is not None else None, "currency": t.currency, "tier_type": t.tier_type,
               "validity_days": t.validity_days, "total_classes": t.total_classes if t.total_classes is not None else (1 if t.tier_type == "DROP_IN" else None),
               "classes": [{"id": str(c.session.id), "name": c.session.name} for c in t.classes
                           if c.session.is_active and c.session.is_public and c.session.deleted_at is None and c.session.organization_id == org.id]}
              for t in tiers]
    return {
        "passes": passes,
        "organization": {
            "id": str(org.id),
            "name": org.name,
            "slug": org.slug,
            "logo_url": org.logo_url,
            "timezone": org.timezone,
            "currency": org.currency
        },
        "website": {**website_content(org.website), "is_published": bool(org.website and org.website.is_published)},
        "instructors": [
            {
                "name": m.user.full_name,
                "role": m.role,
                "title": m.title,
                "bio": m.bio,
                "avatar_url": m.user.avatar_url
            }
            for m in org.members if m.is_active and m.role in ("STUDIO", "TEACHER")
        ],
        "class_types": [
            {"id": str(ct.id), "name": ct.name, "slug": ct.slug}
            for ct in org.class_types if ct.deleted_at is None
        ],
        "sessions": public_sessions
    }

@router.get("/public/organizations/{org_slug}/classes/{session_slug}")
@router.get("/public/organizations/{org_slug}/sessions/{session_slug}", include_in_schema=False)
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
        Organization.status == "ACTIVE",
        Organization.deleted_at.is_(None),
        Session.slug == session_slug,
        Session.is_public == True,
        Session.is_active == True,
        Session.deleted_at.is_(None)
    )
    res = await db.execute(stmt)
    session = res.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    now_utc = datetime.now(timezone.utc)
    next_occ_stmt = select(ClassOccurrence).where(
        ClassOccurrence.session_id == session.id,
        ClassOccurrence.status == "SCHEDULED",
        ClassOccurrence.actual_start_at >= now_utc
    ).order_by(ClassOccurrence.actual_start_at.asc()).limit(1)
    next_occ = (await db.execute(next_occ_stmt)).scalar_one_or_none()

    enrolled_count = (await db.execute(
        select(func.count(SessionEnrollment.id)).where(
            SessionEnrollment.session_id == session.id,
            SessionEnrollment.status == "ACTIVE"
        )
    )).scalar_one() or 0
    remaining_spots = max(0, session.capacity - enrolled_count)

    set_stmt = select(OrganizationSetting).where(OrganizationSetting.organization_id == session.organization_id)
    org_settings = (await db.execute(set_stmt)).scalar_one_or_none()

    teaching_mode = (org_settings.teaching_mode if org_settings else "HYBRID") or "HYBRID"
    delivery_mode = "ONLINE_LIVE" if session.session_type in ("ONLINE", "LIVE") else ("IN_STUDIO" if teaching_mode == "PHYSICAL" else "ONLINE_HYBRID")

    venue_address = ""
    if org_settings and org_settings.address_line1:
        parts = [org_settings.address_line1, org_settings.city, org_settings.state, org_settings.pincode]
        venue_address = ", ".join(p for p in parts if p)
    if not venue_address:
        venue_address = f"{session.organization.name} Studio Sanctuary, Bengaluru"

    venue = {
        "name": f"{session.organization.name} Main Sanctuary",
        "address": venue_address,
        "city": (org_settings.city if org_settings else None) or "Bengaluru",
        "delivery_mode": delivery_mode,
        "is_inherited": True,
        "policy_source": "Inherited Studio Sanctuary Location"
    }

    return {
        "id": str(session.id),
        "name": session.name,
        "slug": session.slug,
        "short_description": session.short_description,
        "full_description": session.full_description,
        "session_type": session.session_type,
        "delivery_mode": delivery_mode,
        "venue": venue,
        "capacity": session.capacity,
        "min_age": session.min_age,
        "max_age": session.max_age,
        "skill_level": session.skill_level,
        "duration_minutes": session.duration_minutes,
        "language": session.language,
        "next_occurrence": {
            "id": str(next_occ.id),
            "start_time": next_occ.actual_start_at.isoformat(),
            "end_time": next_occ.actual_end_at.isoformat()
        } if next_occ else None,
        "availability": {
            "capacity": session.capacity,
            "enrolled_count": enrolled_count,
            "remaining_spots": remaining_spots,
            "is_full": remaining_spots <= 0
        },
        "cancellation_terms": "Free cancellation up to 2 hours before class start. Class credit refunded to pass.",
        "cancellation_policy": {
            "terms": "Free cancellation up to 2 hours before class start. Class credit refunded to pass.",
            "cutoff_hours": 2,
            "is_inherited": True,
            "policy_source": "Inherited Studio-Wide Policy"
        },
        "meeting_readiness": "Protected Zoom video room opens automatically in your Student Portal 15 minutes before class.",
        "renewal_terms": "Single pass purchase. Activates immediately upon checkout. No auto-recurring deductions.",
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
            "total_classes": session.pricing.total_classes if session.pricing.total_classes is not None else (1 if session.pricing.pricing_type in ("ONE_TIME", "PER_SESSION") else None),
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
    org_id: uuid.UUID, payload: WebsiteUpdatePayload,
    tenant=Depends(require_org_role(EDITOR_ROLES)), db: AsyncSession = Depends(get_db)
):
    ws = await load_website(db, org_id)
    if not ws:
        tmpl = (await db.execute(select(WebsiteTemplate).where(WebsiteTemplate.code == payload.template_code))).scalar_one_or_none()
        if not tmpl:
            tmpl = WebsiteTemplate(code=payload.template_code, name=TEMPLATES[payload.template_code])
            db.add(tmpl)
            await db.flush()
        ws = OrganizationWebsite(organization_id=org_id, template_id=tmpl.id, is_published=False)
        db.add(ws)
    set_entry(ws, "website_draft", payload.model_dump())
    await db.commit()
    return {"message": "Draft saved"}


@router.post("/organizations/{org_id}/website/preview")
async def preview_website(org_id: uuid.UUID, payload: WebsiteUpdatePayload, tenant=Depends(require_org_role(EDITOR_ROLES))):
    org, _ = tenant
    html = await rendering.render_document(payload, org.name, org.slug, settings.WEBSITE_PUBLIC_ORIGIN, str(org_id), MEDIA_DIR)
    manifest = rendering.write_version(str(org_id), payload, html, "previews")
    return {"html": html, "version": manifest["version"]}


@router.post("/organizations/{org_id}/website/publish")
async def publish_website(org_id: uuid.UUID, tenant=Depends(require_org_role(EDITOR_ROLES)), db: AsyncSession = Depends(get_db)):
    ws = await load_website(db, org_id)
    draft = get_entry(ws, "website_draft")
    if not draft:
        raise HTTPException(400, "Save a draft before publishing")
    content = WebsiteUpdatePayload.model_validate(draft)
    org, _ = tenant
    html = await rendering.render_document(content, org.name, org.slug, settings.WEBSITE_PUBLIC_ORIGIN, str(org_id), MEDIA_DIR)
    # Finish rendering and writing before altering published fields. Failed rendering
    # leaves the old release untouched; uncommitted files are never publicly served.
    manifest = rendering.write_version(str(org_id), content, html, "versions")
    tmpl = (await db.execute(select(WebsiteTemplate).where(WebsiteTemplate.code == content.template_code))).scalar_one_or_none()
    if not tmpl:
        tmpl = WebsiteTemplate(code=content.template_code, name=TEMPLATES[content.template_code])
        db.add(tmpl)
        await db.flush()
    ws.template_id = tmpl.id
    ws.template = tmpl
    for key in ("headline", "subheadline", "hero_image_url", "about_text", "primary_color", "accent_color"):
        setattr(ws, key, getattr(content, key))
    set_entry(ws, "website_published", content.model_dump())
    set_entry(ws, "website_release", manifest)
    set_entry(ws, "website_versions", get_entry(ws, "website_versions", []) + [manifest])
    ws.is_published = True
    await db.commit()
    return {"message": "Website published", "release": manifest}


@router.get("/organizations/{org_id}/website/download/{version}")
async def download_website(org_id: uuid.UUID, version: str, tenant=Depends(require_org_role(EDITOR_ROLES)), db: AsyncSession = Depends(get_db)):
    ws = await load_website(db, org_id)
    if version not in {item["version"] for item in get_entry(ws, "website_versions", [])}:
        raise HTTPException(404, "Published version not found")
    return FileResponse(rendering.version_path(str(org_id), version), media_type="text/html", filename="studio-website.html", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/public/organizations/{org_slug}/website-html", response_class=HTMLResponse)
async def public_website_html(org_slug: str, db: AsyncSession = Depends(get_db)):
    org = (await db.execute(select(Organization).options(selectinload(Organization.website).selectinload(OrganizationWebsite.template)).where(
        Organization.slug == org_slug, Organization.status == "ACTIVE", Organization.deleted_at.is_(None)
    ))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Studio not found")
    ws = org.website
    if not ws or not ws.is_published:
        raise HTTPException(404, "Website is in Draft state and has not been published yet.")
    release = get_entry(ws, "website_release")
    if not release or not isinstance(release, dict) or not release.get("version"):
        raise HTTPException(404, "Published website artifact not found. Please publish the website first.")
    artifact_path = rendering.version_path(str(org.id), release["version"])
    if not artifact_path.exists():
        raise HTTPException(404, "Published website artifact does not exist on disk. Please publish again.")
    html = artifact_path.read_text()
    return HTMLResponse(html, headers=HTML_HEADERS)

# Set WEBSITE_MEDIA_DIR to a persistent shared volume in multi-instance deployments.
MEDIA_DIR = rendering.MEDIA_DIR

@router.post("/organizations/{org_id}/website/image")
async def upload_website_image(org_id: uuid.UUID, file: UploadFile, tenant=Depends(require_org_role(EDITOR_ROLES))):
    data = await file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "Choose an image smaller than 5 MB")
    extension = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        extension = "jpg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        extension = "webp"
    if not extension:
        raise HTTPException(400, "Choose a PNG, JPEG, or WebP image")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{org_id}-{uuid.uuid4()}.{extension}"
    (MEDIA_DIR / filename).write_bytes(data)
    return {"url": f"/api/v1/public/website-images/{filename}"}

@router.get("/public/website-images/{filename}")
async def website_image(filename: str):
    import re
    if not re.fullmatch(r"(?:[0-9a-f-]{36}-)?[0-9a-f-]{36}\.(png|jpg|webp)", filename):
        raise HTTPException(404, "Image not found")
    path = MEDIA_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})
