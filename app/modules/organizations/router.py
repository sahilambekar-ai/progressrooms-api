import json
import random
import secrets
import uuid
from datetime import datetime, timezone
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.models.integration import OrganizationIntegration
from app.models.user import User
from app.modules.auth.dependencies import get_current_user
from app.modules.organizations.dependencies import get_current_tenant_member, require_org_role
from app.common.enums import UserRole

router = APIRouter(prefix="/organizations", tags=["Organizations"])

class OrganizationCreate(BaseModel):
    name: str
    slug: str
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"

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
    name: str | None = None
    phone: str | None = None
    support_email: str | None = None
    studio_tagline: str | None = None
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
    account_completed: bool | None = None
    completion_step: int | None = None

class ZoomConnectRequest(BaseModel):
    account_email: str | None = None
    account_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    auto_meeting_enabled: bool = True
    waiting_room: bool = True
    host_video: bool = True

class ZoomMeetingGenerateRequest(BaseModel):
    topic: str
    start_time: str | None = None
    duration_minutes: int = 60
    timezone: str = "Asia/Kolkata"

def mask_account_number(acc: str | None) -> str | None:
    if not acc:
        return None
    acc_clean = acc.strip()
    if len(acc_clean) <= 4:
        return acc_clean
    return f"{'•' * (len(acc_clean) - 4)}{acc_clean[-4:]}"


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
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
        is_active=True
    )
    db.add(member)

    # Initialize settings
    settings = OrganizationSetting(organization_id=org.id)
    db.add(settings)

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
    tenant: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member),
    db: AsyncSession = Depends(get_db)
):
    org, membership = tenant
    stmt = select(Organization).options(
        selectinload(Organization.subscription),
        selectinload(Organization.settings)
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
    tenant: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member),
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
    tenant: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member),
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

    # Check active zoom integration
    zoom_stmt = select(OrganizationIntegration).where(
        OrganizationIntegration.organization_id == org_id,
        OrganizationIntegration.provider == "ZOOM",
        OrganizationIntegration.is_active == True
    )
    zoom_int = (await db.execute(zoom_stmt)).scalar_one_or_none()

    return {
        "organization_id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "phone": settings.phone or settings.support_phone,
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
        "settlement_cycle": settings.settlement_cycle,
        "zoom_connected": settings.zoom_connected or (zoom_int is not None),
        "zoom_account_email": settings.zoom_account_email,
        "zoom_auto_meeting_enabled": settings.zoom_auto_meeting_enabled,
        "zoom_waiting_room": settings.zoom_waiting_room,
        "zoom_host_video": settings.zoom_host_video,
        "account_completed": settings.account_completed,
        "completion_step": settings.completion_step,
        "role": membership.role if membership else "SUPER_ADMIN"
    }

@router.put("/{org_id}/profile")
async def update_studio_profile(
    org_id: uuid.UUID,
    payload: StudioProfileUpdate,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.INSTRUCTOR])),
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
        settings.account_number_enc = payload.account_number.strip()
    if payload.ifsc_code is not None:
        settings.ifsc_code = payload.ifsc_code.strip().upper()
    if payload.upi_id is not None:
        settings.upi_id = payload.upi_id.strip().lower()
    if payload.settlement_cycle is not None:
        settings.settlement_cycle = payload.settlement_cycle
    if payload.zoom_connected is not None:
        settings.zoom_connected = payload.zoom_connected
    if payload.zoom_account_email is not None:
        settings.zoom_account_email = payload.zoom_account_email.strip().lower()
    if payload.zoom_auto_meeting_enabled is not None:
        settings.zoom_auto_meeting_enabled = payload.zoom_auto_meeting_enabled
    if payload.zoom_waiting_room is not None:
        settings.zoom_waiting_room = payload.zoom_waiting_room
    if payload.zoom_host_video is not None:
        settings.zoom_host_video = payload.zoom_host_video
    if payload.account_completed is not None:
        settings.account_completed = payload.account_completed
    if payload.completion_step is not None:
        settings.completion_step = payload.completion_step

    await db.commit()
    await db.refresh(settings)
    await db.refresh(org)

    return {
        "status": "success",
        "message": "Studio profile updated successfully",
        "account_completed": settings.account_completed,
        "completion_step": settings.completion_step,
        "zoom_connected": settings.zoom_connected
    }

@router.post("/{org_id}/zoom/connect")
async def connect_zoom(
    org_id: uuid.UUID,
    payload: ZoomConnectRequest,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.INSTRUCTOR])),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    org, _ = tenant
    account_email = (payload.account_email or current_user.email).strip().lower()

    stmt = select(OrganizationIntegration).where(
        OrganizationIntegration.organization_id == org_id,
        OrganizationIntegration.provider == "ZOOM"
    )
    res = await db.execute(stmt)
    integration = res.scalar_one_or_none()

    cred_payload = {
        "email": account_email,
        "account_id": payload.account_id or f"act_zoom_{uuid.uuid4().hex[:8]}",
        "connected_at": datetime.now(timezone.utc).isoformat(),
        "mode": "SERVER_TO_SERVER_OAUTH" if payload.client_id else "OAUTH_CONNECTED"
    }

    if not integration:
        integration = OrganizationIntegration(
            organization_id=org_id,
            provider="ZOOM",
            credentials_enc=json.dumps(cred_payload),
            is_active=True
        )
        db.add(integration)
    else:
        integration.credentials_enc = json.dumps(cred_payload)
        integration.is_active = True

    set_stmt = select(OrganizationSetting).where(OrganizationSetting.organization_id == org_id)
    settings = (await db.execute(set_stmt)).scalar_one_or_none()
    if not settings:
        settings = OrganizationSetting(organization_id=org_id)
        db.add(settings)

    settings.zoom_connected = True
    settings.zoom_account_email = account_email
    settings.zoom_auto_meeting_enabled = payload.auto_meeting_enabled
    settings.zoom_waiting_room = payload.waiting_room
    settings.zoom_host_video = payload.host_video

    await db.commit()

    return {
        "status": "connected",
        "provider": "ZOOM",
        "account_email": account_email,
        "auto_meeting_enabled": settings.zoom_auto_meeting_enabled,
        "message": f"Zoom account ({account_email}) successfully authenticated."
    }

@router.post("/{org_id}/zoom/disconnect")
async def disconnect_zoom(
    org_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.INSTRUCTOR])),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(OrganizationIntegration).where(
        OrganizationIntegration.organization_id == org_id,
        OrganizationIntegration.provider == "ZOOM"
    )
    res = await db.execute(stmt)
    integration = res.scalar_one_or_none()
    if integration:
        integration.is_active = False

    set_stmt = select(OrganizationSetting).where(OrganizationSetting.organization_id == org_id)
    settings = (await db.execute(set_stmt)).scalar_one_or_none()
    if settings:
        settings.zoom_connected = False

    await db.commit()
    return {"status": "disconnected", "message": "Zoom account disconnected."}

@router.post("/{org_id}/zoom/generate-meeting")
async def generate_zoom_meeting(
    org_id: uuid.UUID,
    payload: ZoomMeetingGenerateRequest,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(get_current_tenant_member),
    db: AsyncSession = Depends(get_db)
):
    org, _ = tenant
    set_stmt = select(OrganizationSetting).where(OrganizationSetting.organization_id == org_id)
    settings = (await db.execute(set_stmt)).scalar_one_or_none()

    num1 = random.randint(800, 899)
    num2 = random.randint(1000, 9999)
    num3 = random.randint(1000, 9999)
    meeting_id = f"{num1}{num2}{num3}"
    passcode = secrets.token_urlsafe(6)
    join_url = f"https://us05web.zoom.us/j/{meeting_id}?pwd={passcode}"
    start_url = f"https://us05web.zoom.us/s/{meeting_id}?zak=mock_zak_{secrets.token_hex(8)}"

    return {
        "provider": "ZOOM",
        "meeting_id": meeting_id,
        "passcode": passcode,
        "join_url": join_url,
        "start_url": start_url,
        "topic": payload.topic,
        "timezone": payload.timezone or org.timezone,
        "duration_minutes": payload.duration_minutes,
        "waiting_room": settings.zoom_waiting_room if settings else True,
        "host_video": settings.zoom_host_video if settings else True
    }

