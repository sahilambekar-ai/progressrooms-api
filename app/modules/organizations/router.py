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
from app.modules.organizations.zoom_service import ZoomService, ZoomAuthError

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
    whatsapp_number: str | None = None
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
    completion_percentage: int | None = None

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
    if not acc:
        return None
    acc_clean = acc.strip()
    if len(acc_clean) <= 4:
        return acc_clean
    return f"{'•' * (len(acc_clean) - 4)}{acc_clean[-4:]}"

def compute_completion_percentage(settings: OrganizationSetting, org: Organization) -> int:
    if settings.account_completed:
        return 100
    score = 0
    # Step 1: Studio Name & Mobile Phone (17%)
    if (org.name and org.name.strip()) and (settings.phone and settings.phone.strip()):
        score += 17
    # Step 2: Location (17%)
    if settings.address_line1 and settings.city and settings.state and settings.pincode:
        score += 17
    # Step 3: Legal / GST (16%)
    if settings.legal_business_name and (not settings.has_gst or (settings.has_gst and settings.gst_number)):
        score += 16
    # Step 4: Bank Payouts (17%)
    if settings.bank_name and settings.account_holder_name and settings.account_number_enc and settings.ifsc_code:
        score += 17
    # Step 5: Zoom Video (17%)
    if settings.zoom_connected and settings.zoom_account_email:
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

    # Check active zoom integration
    zoom_stmt = select(OrganizationIntegration).where(
        OrganizationIntegration.organization_id == org_id,
        OrganizationIntegration.provider == "ZOOM",
        OrganizationIntegration.is_active == True
    )
    zoom_int = (await db.execute(zoom_stmt)).scalar_one_or_none()

    comp_pct = 100 if settings.account_completed else (settings.completion_percentage or compute_completion_percentage(settings, org))

    return {
        "organization_id": str(org.id),
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
        "settlement_cycle": settings.settlement_cycle,
        "zoom_connected": settings.zoom_connected or (zoom_int is not None),
        "zoom_account_email": settings.zoom_account_email,
        "zoom_auto_meeting_enabled": settings.zoom_auto_meeting_enabled,
        "zoom_waiting_room": settings.zoom_waiting_room,
        "zoom_host_video": settings.zoom_host_video,
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
        if payload.account_completed:
            settings.completion_step = 6
            settings.completion_percentage = 100
    if payload.completion_step is not None and not settings.account_completed:
        settings.completion_step = payload.completion_step

    if not settings.account_completed:
        settings.completion_percentage = (
            payload.completion_percentage
            if payload.completion_percentage is not None
            else compute_completion_percentage(settings, org)
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
        "zoom_connected": settings.zoom_connected,
        "whatsapp_number": settings.whatsapp_number
    }

@router.post("/{org_id}/zoom/connect")
async def connect_zoom(
    org_id: uuid.UUID,
    payload: ZoomConnectRequest,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.STUDIO])),
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

    if payload.is_dev_simulation:
        cred_payload = {
            "email": account_email,
            "account_id": payload.account_id or f"sim_acc_{uuid.uuid4().hex[:8]}",
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "mode": "DEV_SIMULATION",
            "is_simulated": True,
            "note": "Simulated local connection for development. Real Zoom API calls are bypassed."
        }
    else:
        # Real provider connection requires Account ID, Client ID, and Client Secret
        if not (payload.account_id and payload.client_id and payload.client_secret):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Real Zoom connection requires Account ID, Client ID, and Client Secret from your Zoom Server-to-Server OAuth app. In development, you may enable simulation mode to test without credentials."
            )
        try:
            token_data = await ZoomService.verify_and_obtain_token(
                account_id=payload.account_id.strip(),
                client_id=payload.client_id.strip(),
                client_secret=payload.client_secret.strip()
            )
        except ZoomAuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

        cred_payload = {
            "email": account_email,
            "account_id": payload.account_id.strip(),
            "client_id": payload.client_id.strip(),
            "client_secret": payload.client_secret.strip(),
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "mode": "SERVER_TO_SERVER_OAUTH",
            "is_simulated": False
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

    provider_label = "DEV_SIMULATION" if payload.is_dev_simulation else "ZOOM"
    msg = (
        f"[DEV_SIMULATION] Simulated Zoom provider connected for local testing ({account_email}). Real Zoom calls are bypassed."
        if payload.is_dev_simulation
        else f"Zoom account ({account_email}) successfully authenticated with provider."
    )

    return {
        "status": "connected",
        "provider": provider_label,
        "is_simulated": payload.is_dev_simulation,
        "account_email": account_email,
        "auto_meeting_enabled": settings.zoom_auto_meeting_enabled,
        "message": msg
    }

@router.post("/{org_id}/zoom/disconnect")
async def disconnect_zoom(
    org_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.STUDIO])),
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
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.STUDIO])),
    db: AsyncSession = Depends(get_db)
):
    org, _ = tenant
    set_stmt = select(OrganizationSetting).where(OrganizationSetting.organization_id == org_id)
    settings = (await db.execute(set_stmt)).scalar_one_or_none()

    if not settings or not settings.zoom_connected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Zoom is Not Connected. You must connect Zoom before generating test meetings."
        )

    stmt = select(OrganizationIntegration).where(
        OrganizationIntegration.organization_id == org_id,
        OrganizationIntegration.provider == "ZOOM",
        OrganizationIntegration.is_active.is_(True)
    )
    integration = (await db.execute(stmt)).scalar_one_or_none()
    if not integration or not integration.credentials_enc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Zoom credentials not found. Please reconnect Zoom."
        )

    try:
        cred_data = json.loads(integration.credentials_enc)
    except Exception:
        cred_data = {}

    is_simulated = cred_data.get("is_simulated", False) or cred_data.get("mode") == "DEV_SIMULATION"

    if is_simulated:
        sim_id = f"sim_{secrets.randbelow(900_000_000) + 100_000_000}"
        passcode = secrets.token_urlsafe(6)
        join_url = f"https://dev-simulation.zoom.us/j/{sim_id}?pwd={passcode}"
        start_url = f"https://dev-simulation.zoom.us/s/{sim_id}?zak=sim_zak_{secrets.token_hex(8)}"

        return {
            "provider": "DEV_SIMULATION",
            "is_simulated": True,
            "meeting_id": sim_id,
            "passcode": passcode,
            "join_url": join_url,
            "start_url": start_url,
            "topic": payload.topic,
            "timezone": payload.timezone or org.timezone,
            "duration_minutes": payload.duration_minutes,
            "waiting_room": settings.zoom_waiting_room if settings else True,
            "host_video": settings.zoom_host_video if settings else True,
            "message": "[DEV_SIMULATION] Sample meeting generated for local development."
        }

    # Real Zoom Meeting Generation
    account_id = cred_data.get("account_id")
    client_id = cred_data.get("client_id")
    client_secret = cred_data.get("client_secret")

    if not (account_id and client_id and client_secret):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incomplete Zoom credentials: Account ID, Client ID, and Client Secret are required."
        )

    try:
        token_info = await ZoomService.verify_and_obtain_token(account_id, client_id, client_secret)
        meeting = await ZoomService.create_meeting(
            access_token=token_info["access_token"],
            topic=payload.topic,
            start_time=datetime.now(timezone.utc),
            duration_minutes=payload.duration_minutes,
            timezone_str=payload.timezone or org.timezone,
            waiting_room=settings.zoom_waiting_room if settings else True,
            host_video=settings.zoom_host_video if settings else True,
            host_email=settings.zoom_account_email or "me"
        )
    except ZoomAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    return {
        "provider": "ZOOM",
        "is_simulated": False,
        "meeting_id": meeting["meeting_id"],
        "passcode": meeting["passcode"],
        "join_url": meeting["join_url"],
        "start_url": meeting["start_url"],
        "topic": payload.topic,
        "timezone": payload.timezone or org.timezone,
        "duration_minutes": payload.duration_minutes,
        "waiting_room": settings.zoom_waiting_room if settings else True,
        "host_video": settings.zoom_host_video if settings else True,
        "message": "Live Zoom meeting room provisioned successfully."
    }


@router.get("/{org_id}/analytics")
async def get_studio_analytics(
    org_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    tenant=Depends(require_org_role([UserRole.STUDIO])),
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import func
    from app.models.commerce import SessionEnrollment, Order, Payment
    from app.models.session import Session, SessionPricing
    from app.models.support import SupportTicket
    from app.models.membership_tier import MembershipTier

    # 1. Active enrolled students: DISTINCT user_id with an ACTIVE SessionEnrollment in this studio
    active_students_count = await db.scalar(
        select(func.count(func.distinct(SessionEnrollment.user_id)))
        .where(
            SessionEnrollment.organization_id == org_id,
            SessionEnrollment.status == "ACTIVE"
        )
    ) or 0

    # 2. Active recurring batches: configured active sessions in this studio
    active_batches_count = await db.scalar(
        select(func.count(Session.id))
        .where(
            Session.organization_id == org_id,
            Session.is_active == True,
            Session.deleted_at.is_(None)
        )
    ) or 0

    # 3. Total support tickets in this studio
    support_tickets_count = await db.scalar(
        select(func.count(SupportTicket.id))
        .where(SupportTicket.organization_id == org_id)
    ) or 0

    # 4. Projected subscription value from active class batches and passes
    classes_pricing_sum = await db.scalar(
        select(func.sum(SessionPricing.price))
        .join(Session, Session.id == SessionPricing.session_id)
        .where(
            Session.organization_id == org_id,
            Session.is_active == True,
            Session.deleted_at.is_(None)
        )
    ) or 0

    passes_pricing_sum = await db.scalar(
        select(func.sum(MembershipTier.price))
        .where(
            MembershipTier.organization_id == org_id,
            MembershipTier.is_active == True,
            MembershipTier.deleted_at.is_(None)
        )
    ) or 0

    projected_subscription_value = float(classes_pricing_sum or 0) + float(passes_pricing_sum or 0)

    # 5. Collected gateway revenue (Payment.status in ('SUCCESS', 'CAPTURED'))
    collected_revenue = await db.scalar(
        select(func.sum(Payment.amount))
        .join(Order, Payment.order_id == Order.id)
        .where(
            Order.organization_id == org_id,
            Payment.status.in_(["SUCCESS", "CAPTURED"])
        )
    ) or 0

    # 6. Billed revenue: sum of billing_value across active enrollments
    billed_revenue = await db.scalar(
        select(func.sum(SessionEnrollment.billing_value))
        .where(
            SessionEnrollment.organization_id == org_id,
            SessionEnrollment.status == "ACTIVE"
        )
    ) or 0

    return {
        "active_enrolled_students": active_students_count,
        "active_recurring_batches": active_batches_count,
        "total_support_queries": support_tickets_count,
        "projected_subscription_value": projected_subscription_value,
        "collected_revenue": float(collected_revenue),
        "billed_revenue": float(billed_revenue),
        "currency": tenant[0].currency or "INR",
        "explanation": "Billed revenue reflects total scheduled subscription value across student enrollments. Collected revenue reflects actual gateway payment receipts."
    }

