import json
import random
import secrets
import uuid
from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator
from app.common.validation import Name, Slug, Currency
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.models.integration import OrganizationIntegration
from app.modules.teachers.codes import generate_teacher_code
from app.models.user import User
from app.modules.auth.dependencies import get_current_user
from app.modules.organizations.dependencies import get_current_tenant_member, require_org_role
from app.common.enums import UserRole
from app.modules.zoom.router import DefaultsRequest, get_defaults, save_defaults
from app.modules.zoom import service as zoom_accounts

router = APIRouter(prefix="/organizations", tags=["Organizations"])

class OrganizationCreate(BaseModel):
    name: Name
    slug: Slug
    timezone: str = "Asia/Kolkata"
    currency: Currency = "INR"

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Choose a valid IANA timezone")
        return value

class OrganizationUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    currency: str | None = None
    logo_url: str | None = None

class MemberInvite(BaseModel):
    email: str
    full_name: str
    role: UserRole = UserRole.INSTRUCTOR
    title: str | None = None
    bio: str | None = None

class StudioProfileUpdate(BaseModel):
    name: Name | None = None
    phone: str | None = None
    whatsapp_number: str | None = None
    support_email: str | None = None
    studio_tagline: str | None = Field(default=None, max_length=5000)
    disciplines: str | None = None
    teaching_mode: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    country: str | None = "India"
    has_gst: bool | None = None
    gst_number: str | None = None
    legal_business_name: str | None = None
    pan_number: str | None = None
    bank_name: str | None = None
    account_holder_name: str | None = None
    account_number: str | None = None
    ifsc_code: str | None = None
    upi_id: str | None = None
    settlement_cycle: str | None = None
    zoom_connected: bool | None = None
    zoom_account_email: str | None = None
    zoom_auto_meeting_enabled: bool | None = None
    zoom_waiting_room: bool | None = None
    zoom_host_video: bool | None = None
    zoom_enforce_basic_login: bool | None = None
    zoom_enforce_zoom_login: bool | None = None
    zoom_group_allow_self_buy_subscriptions: bool | None = None
    zoom_group_active_subscription_required: bool | None = None
    zoom_group_confirm_checkin_everytime: bool | None = None
    zoom_personal_allow_self_buy_subscriptions: bool | None = None
    zoom_personal_active_subscription_required: bool | None = None
    zoom_personal_confirm_checkin_everytime: bool | None = None
    zoom_personal_no_show_penalty: bool | None = None
    zoom_join_before_host: bool | None = None
    zoom_cloud_recording_mode: str | None = None
    zoom_host_key: str | None = None
    account_completed: bool | None = None
    completion_step: int | None = Field(default=None, ge=1, le=6)
    completion_percentage: int | None = Field(default=None, ge=0, le=100)

    @field_validator("*")
    @classmethod
    def match_database_lengths(cls, value, info):
        if isinstance(value, str):
            column = OrganizationSetting.__table__.columns.get(info.field_name)
            limit = getattr(column.type, "length", None) if column is not None else None
            # Plain input is encrypted before persisting, so reserve space for
            # the stored representation instead of accepting unbounded values.
            if info.field_name == "account_number":
                limit = 100
            if limit and len(value) > limit:
                raise ValueError(f"Use at most {limit} characters")
        return value

class ZoomSettingsUpdateRequest(BaseModel):
    enforce_basic_login: bool | None = None
    enforce_zoom_login: bool | None = None
    waiting_room: bool | None = None
    group_allow_self_buy_subscriptions: bool | None = None
    group_active_subscription_required: bool | None = None
    group_confirm_checkin_everytime: bool | None = None
    personal_allow_self_buy_subscriptions: bool | None = None
    personal_active_subscription_required: bool | None = None
    personal_confirm_checkin_everytime: bool | None = None
    personal_no_show_penalty: bool | None = None
    join_before_host: bool | None = None
    cloud_recording_mode: str | None = None
    host_key: str | None = None

class ZoomHostKeyUpdateRequest(BaseModel):
    host_key: str = Field(pattern=r"^\d{6}$")

class ZoomConnectRequest(BaseModel):
    account_email: str | None = None
    account_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    auto_meeting_enabled: bool = True
    waiting_room: bool = True
    host_video: bool = True
    is_dev_simulation: bool = False

class ZoomMeetingGenerateRequest(BaseModel):
    topic: str
    start_time: str | None = None
    duration_minutes: int = 60
    timezone: str = "Asia/Kolkata"

def mask_account_number(acc: str | None) -> str | None:
    from app.core.financial_data import mask_account
    return mask_account(acc)


def compute_completion_percentage(settings: OrganizationSetting, org: Organization, zoom_ready: bool = False) -> int:
    if settings.account_completed:
        return 100
    score = 0
    # Step 1: Studio Name & Mobile Phone (17%)
    if (org.name and org.name.strip()) and (settings.phone and settings.phone.strip()):
        score += 17
    # Step 2: Location (17%)
    score += 17  # Location is optional.
    # Step 3: Legal / GST (16%)
    score += 16  # Tax details are optional.
    # Step 4: Bank Payouts (17%)
    if settings.bank_name and settings.account_holder_name and settings.account_number_enc and settings.ifsc_code:
        score += 17
    # Step 5: Zoom Video (17%)
    if zoom_ready:
        score += 17
    # Step 6: Full Launch Review
    if settings.completion_step >= 6 and settings.account_completed:
        return 100
    return min(score, 84)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if current_user.role != "SUPER_ADMIN":
        raise HTTPException(403, "Studios are created through studio registration")
    # Check slug
    existing = await db.execute(select(Organization).where(Organization.slug == payload.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Slug already taken")

    org = Organization(
        name=payload.name,
        slug=payload.slug,
        timezone=payload.timezone,
        currency=payload.currency
    )
    db.add(org)
    await db.flush()

    # Create Owner membership
    member = OrganizationMember(
        organization_id=org.id,
        user_id=current_user.id,
        role=UserRole.OWNER.value,
        title="Founder / Owner",
        can_teach=True,
        teacher_code=await generate_teacher_code(db),
        is_active=True
    )
    db.add(member)

    # Initialize settings
    settings = OrganizationSetting(organization_id=org.id)
    db.add(settings)

    from app.modules.billing.saas import initialize_subscription
    await initialize_subscription(db, org.id, verified=True)
    await db.commit()
    await db.refresh(org)

    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "timezone": org.timezone,
        "currency": org.currency
    }

@router.get("/{org_id}")
async def get_organization(
    org_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.STUDIO])),
    db: AsyncSession = Depends(get_db)
):
    org, membership = tenant
    stmt = select(Organization).options(
        selectinload(Organization.subscription)
    ).where(Organization.id == org_id)
    res = await db.execute(stmt)
    full_org = res.scalar_one()

    return {
        "id": str(full_org.id),
        "name": full_org.name,
        "slug": full_org.slug,
        "timezone": full_org.timezone,
        "currency": full_org.currency,
        "status": full_org.status,
        "role": membership.role if membership else "SUPER_ADMIN",
        "subscription": {
            "status": full_org.subscription.status,
            "starts_at": full_org.subscription.starts_at.isoformat() if full_org.subscription else None,
            "ends_at": full_org.subscription.ends_at.isoformat() if full_org.subscription and full_org.subscription.ends_at else None,
            "trial_ends_at": full_org.subscription.trial_ends_at.isoformat() if full_org.subscription and full_org.subscription.trial_ends_at else None,
        } if full_org.subscription else None
    }

@router.get("/{org_id}/members")
async def list_members(
    org_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.STUDIO])),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(OrganizationMember).options(selectinload(OrganizationMember.user)).where(
        OrganizationMember.organization_id == org_id,
        OrganizationMember.is_active == True
    )
    res = await db.execute(stmt)
    members = res.scalars().all()
    return [
        {
            "id": str(m.id),
            "user_id": str(m.user_id),
            "full_name": m.user.full_name,
            "email": m.user.email,
            "role": m.role,
            "title": m.title,
            "bio": m.bio
        }
        for m in members
    ]

@router.get("/{org_id}/profile")
async def get_studio_profile(
    org_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.STUDIO])),
    db: AsyncSession = Depends(get_db)
):
    org, membership = tenant
    stmt = select(OrganizationSetting).where(OrganizationSetting.organization_id == org_id)
    res = await db.execute(stmt)
    settings = res.scalar_one_or_none()
    if not settings:
        settings = OrganizationSetting(organization_id=org_id)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)

    zoom = await get_defaults(db, org_id)
    from app.modules.zoom import service as zoom_service
    initial_zoom = await zoom_service.connection_for(db, membership.id) if membership else None
    try:
        initial_zoom_ready = await zoom_service.eligible(db, initial_zoom)
    except HTTPException:
        initial_zoom_ready = False


    comp_pct = 100 if settings.account_completed else (settings.completion_percentage or compute_completion_percentage(settings, org, initial_zoom_ready))

    return {
        "organization_id": str(org.id),
        "timezone": org.timezone,
        "name": org.name,
        "slug": org.slug,
        "phone": settings.phone or settings.support_phone,
        "whatsapp_number": settings.whatsapp_number or settings.phone or settings.support_phone,
        "support_email": settings.support_email,
        "studio_tagline": settings.studio_tagline,
        "disciplines": settings.disciplines,
        "teaching_mode": settings.teaching_mode,
        "address_line1": settings.address_line1,
        "address_line2": settings.address_line2,
        "city": settings.city,
        "state": settings.state,
        "pincode": settings.pincode,
        "country": settings.country,
        "has_gst": settings.has_gst,
        "gst_number": settings.gst_number,
        "legal_business_name": settings.legal_business_name,
        "pan_number": settings.pan_number,
        "bank_name": settings.bank_name,
        "account_holder_name": settings.account_holder_name,
        "account_number_masked": mask_account_number(settings.account_number_enc),
        "ifsc_code": settings.ifsc_code,
        "upi_id": settings.upi_id,
        "settlement_cycle": "MONTHLY",
        "zoom_connected": initial_zoom_ready,
        "zoom_provider_mode": zoom["provider_mode"],
        "zoom_production_ready": zoom["production_ready"],
        "zoom_account_email": initial_zoom.email if initial_zoom_ready else None,
        "zoom_auto_meeting_enabled": settings.zoom_auto_meeting_enabled,
        "zoom_waiting_room": settings.zoom_waiting_room,
        "zoom_host_video": settings.zoom_host_video,
        "zoom_enforce_basic_login": settings.zoom_enforce_basic_login,
        "zoom_enforce_zoom_login": settings.zoom_enforce_zoom_login,
        "zoom_group_allow_self_buy_subscriptions": settings.zoom_group_allow_self_buy_subscriptions,
        "zoom_group_active_subscription_required": settings.zoom_group_active_subscription_required,
        "zoom_group_confirm_checkin_everytime": settings.zoom_group_confirm_checkin_everytime,
        "zoom_personal_allow_self_buy_subscriptions": settings.zoom_personal_allow_self_buy_subscriptions,
        "zoom_personal_active_subscription_required": settings.zoom_personal_active_subscription_required,
        "zoom_personal_confirm_checkin_everytime": settings.zoom_personal_confirm_checkin_everytime,
        "zoom_personal_no_show_penalty": settings.zoom_personal_no_show_penalty,
        "zoom_join_before_host": settings.zoom_join_before_host,
        "zoom_cloud_recording_mode": settings.zoom_cloud_recording_mode,
        "zoom_host_key": None,
        "account_completed": settings.account_completed,
        "completion_step": settings.completion_step,
        "completion_percentage": comp_pct,
        "role": membership.role if membership else "SUPER_ADMIN"
    }

@router.put("/{org_id}/profile")
async def update_studio_profile(
    org_id: uuid.UUID,
    payload: StudioProfileUpdate,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.STUDIO])),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    org, membership = tenant
    stmt = select(OrganizationSetting).where(OrganizationSetting.organization_id == org_id)
    res = await db.execute(stmt)
    settings = res.scalar_one_or_none()
    if not settings:
        settings = OrganizationSetting(organization_id=org_id)
        db.add(settings)

    if payload.name is not None and payload.name.strip():
        org.name = payload.name.strip()
    if payload.phone is not None:
        settings.phone = payload.phone.strip()
        settings.support_phone = payload.phone.strip()
        if isinstance(current_user, User) and not current_user.is_superadmin:
            current_user.phone = payload.phone.strip()
    if payload.whatsapp_number is not None:
        settings.whatsapp_number = payload.whatsapp_number.strip()
    if payload.support_email is not None:
        settings.support_email = payload.support_email.strip()
    if payload.studio_tagline is not None:
        settings.studio_tagline = payload.studio_tagline.strip()
    if payload.disciplines is not None:
        settings.disciplines = payload.disciplines.strip()
    if payload.teaching_mode is not None:
        settings.teaching_mode = payload.teaching_mode
    if payload.address_line1 is not None:
        settings.address_line1 = payload.address_line1.strip()
    if payload.address_line2 is not None:
        settings.address_line2 = payload.address_line2.strip()
    if payload.city is not None:
        settings.city = payload.city.strip()
    if payload.state is not None:
        settings.state = payload.state.strip()
    if payload.pincode is not None:
        settings.pincode = payload.pincode.strip()
    if payload.country is not None:
        settings.country = payload.country.strip()
    if payload.has_gst is not None:
        settings.has_gst = payload.has_gst
    if payload.gst_number is not None:
        settings.gst_number = payload.gst_number.strip().upper() if payload.gst_number.strip() else None
        if settings.gst_number:
            settings.has_gst = True
    if payload.legal_business_name is not None:
        settings.legal_business_name = payload.legal_business_name.strip()
    if payload.pan_number is not None:
        settings.pan_number = payload.pan_number.strip().upper() if payload.pan_number.strip() else None
    if payload.bank_name is not None:
        settings.bank_name = payload.bank_name.strip()
    if payload.account_holder_name is not None:
        settings.account_holder_name = payload.account_holder_name.strip()
    if payload.account_number is not None and payload.account_number.strip():
        from app.core.financial_data import encrypt_account
        settings.account_number_enc = encrypt_account(payload.account_number.strip())
    if payload.ifsc_code is not None:
        settings.ifsc_code = payload.ifsc_code.strip().upper()
    if payload.upi_id is not None:
        settings.upi_id = payload.upi_id.strip().lower()
    if payload.settlement_cycle is not None:
        settings.settlement_cycle = "MONTHLY"
    # Connection identity and meeting defaults are managed only through their dedicated APIs.
    if any(key.startswith("zoom_") for key in payload.model_fields_set):
        raise HTTPException(422, "Use Zoom Settings and teacher authorization to change Zoom configuration")

    if payload.account_completed is not None:
        if payload.account_completed:
            from app.modules.zoom import service as zoom_service
            connection = await zoom_service.connection_for(db, membership.id) if membership else None
            try:
                ready = await zoom_service.eligible(db, connection)
            except HTTPException:
                ready = False
            if not ready:
                raise HTTPException(422, "Authenticate your initial Zoom account before completing Studio setup")
            settings.completion_step = 6
            settings.completion_percentage = 100
        settings.account_completed = payload.account_completed
    if payload.completion_step is not None and not settings.account_completed:
        settings.completion_step = payload.completion_step

    if not settings.account_completed:
        settings.completion_percentage = (
            payload.completion_percentage
            if payload.completion_percentage is not None
            else compute_completion_percentage(settings, org, (await get_defaults(db, org_id))["zoom_connected"])
        )

    await db.commit()
    await db.refresh(settings)
    await db.refresh(org)

    return {
        "status": "success",
        "message": "Studio profile updated successfully",
        "account_completed": settings.account_completed,
        "completion_step": settings.completion_step,
        "completion_percentage": settings.completion_percentage,
        "zoom_connected": (await get_defaults(db, org_id))["zoom_connected"],
        "whatsapp_number": settings.whatsapp_number
    }

@router.post("/{org_id}/zoom/connect")
async def connect_zoom(org_id: uuid.UUID, payload: ZoomConnectRequest,
    tenant=Depends(require_org_role([UserRole.STUDIO])), current_user=Depends(get_current_user), db=Depends(get_db)):
    raise HTTPException(410, "Connect individual teacher accounts through Zoom Settings; manual S2S credentials are retired")


@router.get("/{org_id}/zoom/settings")
async def get_zoom_settings(org_id: uuid.UUID, tenant=Depends(require_org_role([UserRole.STUDIO])), db=Depends(get_db)):
    return await get_defaults(db, org_id)


@router.put("/{org_id}/zoom/settings")
async def update_zoom_settings(org_id: uuid.UUID, payload: DefaultsRequest,
    tenant=Depends(require_org_role([UserRole.STUDIO])), db=Depends(get_db)):
    return await save_defaults(db, org_id, payload)


@router.put("/{org_id}/zoom/host-key")
async def update_zoom_host_key(org_id: uuid.UUID, payload: ZoomHostKeyUpdateRequest,
    tenant=Depends(require_org_role([UserRole.STUDIO])), db=Depends(get_db)):
    from app.modules.zoom import service as zoom_service
    from app.modules.zoom.provider import seal
    _, member = tenant
    connection = await zoom_service.connection_for(db, member.id) if member else None
    if not await zoom_service.eligible(db, connection):
        raise HTTPException(422, "Authenticate the Studio Zoom account before saving its host key")
    connection.host_key_enc = seal({"host_key": payload.host_key, "zoom_user_id": connection.zoom_user_id})
    await db.commit()
    return {"host_key_configured": True, "email": connection.email}


@router.get("/{org_id}/zoom/host-key")
async def get_zoom_host_key_status(org_id: uuid.UUID,
    tenant=Depends(require_org_role([UserRole.STUDIO])), db=Depends(get_db)):
    from app.modules.zoom import service as zoom_service
    _, member = tenant
    connection = await zoom_service.connection_for(db, member.id) if member else None
    return {"host_key_configured": bool(connection and connection.host_key_enc),
            "email": connection.email if connection else None}


@router.post("/{org_id}/zoom/disconnect")
async def disconnect_zoom(org_id: uuid.UUID, tenant=Depends(require_org_role([UserRole.STUDIO])), db=Depends(get_db)):
    raise HTTPException(410, "Disconnect the intended teacher connection after reviewing its class impact in Zoom Settings")


@router.post("/{org_id}/zoom/generate-meeting")
async def generate_zoom_meeting(org_id: uuid.UUID, payload: ZoomMeetingGenerateRequest,
    tenant=Depends(require_org_role([UserRole.STUDIO])), db=Depends(get_db)):
    raise HTTPException(410, "Use a scheduled class to test Zoom; meetings require a saved class host and audit history")


class AnalyticsResponse(BaseModel):
    active_enrolled_students: int
    active_recurring_batches: int
    total_support_queries: int
    projected_subscription_value: float
    collected_revenue: float
    billed_revenue: float
    currency: str
    explanation: str


@router.get("/{org_id}/analytics", response_model=AnalyticsResponse)
async def studio_analytics(org_id: uuid.UUID,
    tenant=Depends(require_org_role([UserRole.STUDIO])), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func
    from app.models.commerce import SessionEnrollment, Order, Payment
    from app.models.session import Session, SessionPricing
    from app.models.schedule import SessionScheduleRule
    from app.models.support import SupportTicket
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    org = tenant[0]
    active = await db.scalar(select(func.count(func.distinct(SessionEnrollment.user_id))).where(
        SessionEnrollment.organization_id == org_id, SessionEnrollment.status == "ACTIVE",
        SessionEnrollment.valid_from <= now,
        (SessionEnrollment.valid_until.is_(None)) | (SessionEnrollment.valid_until >= now))) or 0
    classes = [Session.organization_id == org_id, Session.deleted_at.is_(None), Session.is_active.is_(True)]
    recurring = await db.scalar(select(func.count(Session.id)).where(*classes, Session.id.in_(
        select(SessionScheduleRule.session_id).where(SessionScheduleRule.is_active.is_(True))))) or 0
    projected = await db.scalar(select(func.sum(SessionPricing.price)).join(Session).where(
        *classes, SessionPricing.currency == org.currency)) or 0
    collected = await db.scalar(select(func.sum(Payment.amount)).join(Order).where(
        Order.organization_id == org_id, Order.status == "PAID", Payment.status == "SUCCESS",
        Payment.currency == org.currency, Order.currency == org.currency)) or 0
    billed = await db.scalar(select(func.sum(Order.total_amount)).where(Order.organization_id == org_id,
        Order.status.in_(["PENDING", "PAID"]), Order.currency == org.currency)) or 0
    tickets = await db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.organization_id == org_id)) or 0
    return AnalyticsResponse(active_enrolled_students=active, active_recurring_batches=recurring,
        total_support_queries=tickets, projected_subscription_value=float(projected),
        collected_revenue=float(collected), billed_revenue=float(billed), currency=org.currency,
        explanation="Financial totals use this studio's currency only. Collected revenue is successful payments on paid orders; billed revenue is pending plus paid orders. Catalog value sums current active class prices and is not a revenue forecast. Manual admissions are not recorded as payments.")
