from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.modules.auth.schemas import (
    RegisterRequest,
    RegisterResponse,
    LoginRequest,
    LoginResponse,
    RequestOTPRequest,
    RequestOTPResponse,
    VerifyOTPRequest,
    TokenResponse,
    UserResponse
)
from app.modules.auth.service import AuthService
from app.modules.auth.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/register", response_model=RegisterResponse)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    otp_code, record_id, msg = await AuthService.register_user(
        db=db,
        full_name=payload.full_name,
        email=payload.email,
        password=payload.password,
        phone=payload.phone,
        role=payload.role,
        studio_name=payload.studio_name
    )
    return RegisterResponse(
        message=msg,
        email=payload.email.strip().lower(),
        is_verified=False,
        otp_record_id=record_id,
        otp_preview=otp_code
    )

@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await AuthService.login_with_password(
        db=db,
        email=payload.email,
        password=payload.password
    )
    return LoginResponse(**result)

@router.post("/request-otp", response_model=RequestOTPResponse)
@router.post("/otp/generate", response_model=RequestOTPResponse)
async def request_otp(payload: RequestOTPRequest, db: AsyncSession = Depends(get_db)):
    otp_code, record_id, is_verified, requires_activation, msg = await AuthService.request_otp(
        db=db,
        email=payload.email,
        purpose=payload.purpose
    )
    return RequestOTPResponse(
        message=msg,
        otp_record_id=record_id,
        otp_preview=otp_code,
        is_verified=is_verified,
        requires_activation=requires_activation
    )

@router.post("/verify-otp", response_model=TokenResponse)
@router.post("/otp/verify", response_model=TokenResponse)
async def verify_otp(payload: VerifyOTPRequest, db: AsyncSession = Depends(get_db)):
    token, user = await AuthService.verify_otp(
        db=db,
        email=payload.email,
        otp_code=payload.code,
        purpose=payload.purpose,
        full_name=payload.full_name
    )
    user_role = "SUPER_ADMIN" if user.is_superadmin else "STUDENT"
    org_id = None
    org_name = None
    org_slug = None
    account_completed = False
    completion_step = 1
    completion_percentage = 0
    zoom_connected = False

    mem_stmt = select(OrganizationMember, Organization).join(
        Organization, Organization.id == OrganizationMember.organization_id
    ).where(OrganizationMember.user_id == user.id)
    mem_res = await db.execute(mem_stmt)
    mem_row = mem_res.first()
    if mem_row:
        member, org = mem_row
        user_role = member.role
        org_id = str(org.id)
        org_name = org.name
        org_slug = org.slug

        set_stmt = select(OrganizationSetting).where(OrganizationSetting.organization_id == org.id)
        org_settings = (await db.execute(set_stmt)).scalar_one_or_none()
        if org_settings:
            account_completed = org_settings.account_completed
            completion_step = org_settings.completion_step
            completion_percentage = org_settings.completion_percentage or 0
            zoom_connected = org_settings.zoom_connected
        else:
            new_settings = OrganizationSetting(
                organization_id=org.id,
                phone=user.phone,
                support_phone=user.phone,
                support_email=user.email,
                account_completed=False,
                completion_step=1,
                completion_percentage=0
            )
            db.add(new_settings)
            await db.commit()

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user={
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "is_superadmin": user.is_superadmin,
            "role": user_role,
            "organization_id": org_id,
            "organization_name": org_name,
            "organization_slug": org_slug,
            "is_verified": getattr(user, "is_verified", True),
            "account_status": getattr(user, "account_status", "ACTIVE"),
            "account_completed": account_completed,
            "completion_step": completion_step,
            "completion_percentage": completion_percentage,
            "zoom_connected": zoom_connected
        }
    )

@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user
