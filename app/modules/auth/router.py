from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.config import settings
from app.core.security import create_access_token
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.modules.auth.schemas import (
    RegisterRequest,
    RegisterResponse,
    LoginRequest,
    DevLoginRequest,
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
        whatsapp_number=payload.whatsapp_number,
        role=payload.role,
        studio_name=payload.studio_name,
        location=payload.location,
        privacy_policy_version=payload.privacy_policy_version
    )
    return RegisterResponse(
        message=msg,
        email=payload.email.strip().lower(),
        is_verified=False,
        otp_record_id=record_id,
        otp_preview=otp_code if settings.AUTH_SHOW_OTP_PREVIEW else None
    )

@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await AuthService.login_with_password(
        db=db,
        email=payload.email,
        password=payload.password
    )
    return LoginResponse(**result)

_DEV_TEST_ACCOUNTS = {
    "SUPER_ADMIN": "admin@test.progressrooms.local",
    "STUDIO": "owner@yogastudio.test",
    "TEACHER": "ananya@yogastudio.test",
    "STUDENT": "student1@test.progressrooms.local",
}

@router.post("/dev-login", response_model=LoginResponse, include_in_schema=False)
async def dev_login(payload: DevLoginRequest, db: AsyncSession = Depends(get_db)):
    """Sign in a seeded test user. This route is unavailable unless explicitly enabled."""
    if not settings.DEV_TEST_LOGIN_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Development login is unavailable")

    email = _DEV_TEST_ACCOUNTS.get(payload.role)
    if not email:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown test role")

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not user or user.role != payload.role:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Seed the development test accounts before using one-click login")

    user.is_active = True
    user.is_verified = True
    user.account_status = "ACTIVE"
    from datetime import datetime, timezone
    user.last_login_at = datetime.now(timezone.utc)

    member = None
    org = None
    member_rows = []
    if payload.role in {"STUDIO", "TEACHER"}:
        member_rows = (await db.execute(select(OrganizationMember, Organization).join(Organization).where(
            OrganizationMember.user_id == user.id,
            OrganizationMember.role == user.role,
            OrganizationMember.is_active == True,
            Organization.deleted_at.is_(None),
            Organization.status == "ACTIVE",
        ).order_by(OrganizationMember.created_at))).all()
        if not member_rows:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The test account has no active studio membership")
        member, org = member_rows[0]

    org_settings = None
    if org:
        org_settings = await db.scalar(select(OrganizationSetting).where(OrganizationSetting.organization_id == org.id))

    await db.commit()
    token = create_access_token({"sub": str(user.id), "email": user.email, "role": user.role, "is_superadmin": user.is_superadmin})
    return LoginResponse(
        access_token=token,
        is_verified=True,
        message="Development test login successful.",
        user={
            "id": str(user.id), "email": user.email, "full_name": user.full_name,
            "is_superadmin": user.is_superadmin, "role": user.role,
            "organization_id": str(org.id) if org else None,
            "organization_name": org.name if org else None,
            "organization_slug": org.slug if org else None,
            "is_verified": True, "account_status": "ACTIVE",
            "account_completed": bool(org_settings and org_settings.account_completed),
            "completion_step": org_settings.completion_step if org_settings else 1,
            "completion_percentage": org_settings.completion_percentage if org_settings else 0,
            "zoom_connected": bool(org_settings and org_settings.zoom_connected),
            "teacher_code": member.teacher_code if member and user.role == "TEACHER" else None,
            "organizations": [
                {
                    "id": str(r[1].id),
                    "name": r[1].name,
                    "slug": r[1].slug,
                    "teacher_code": r[0].teacher_code if user.role == "TEACHER" else None,
                }
                for r in member_rows
            ],
        },
    )

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
        otp_preview=otp_code if settings.AUTH_SHOW_OTP_PREVIEW else None,
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
    user_role = user.role
    org_id = None
    org_name = None
    org_slug = None
    account_completed = False
    completion_step = 1
    completion_percentage = 0
    zoom_connected = False
    teacher_code = None

    mem_stmt = select(OrganizationMember, Organization).join(
        Organization, Organization.id == OrganizationMember.organization_id
    ).where(
        OrganizationMember.user_id == user.id,
        OrganizationMember.is_active == True,
        Organization.deleted_at.is_(None),
        Organization.status == "ACTIVE"
    ).order_by(OrganizationMember.created_at)
    mem_res = await db.execute(mem_stmt)
    mem_rows = mem_res.all()
    if mem_rows:
        member, org = mem_rows[0]
        teacher_code = member.teacher_code if user.role == "TEACHER" else None
        user_role = user.role
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
            "zoom_connected": zoom_connected,
            "teacher_code": teacher_code,
            "organizations": [
                {
                    "id": str(r[1].id),
                    "name": r[1].name,
                    "slug": r[1].slug,
                    "teacher_code": r[0].teacher_code if user.role == "TEACHER" else None,
                }
                for r in mem_rows
            ],
        }
    )

@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user

from app.common.phone import PhoneNumber
from pydantic import BaseModel, Field

class ProfileUpdate(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    phone: PhoneNumber | None = None
    whatsapp_number: PhoneNumber | None = None
    address: str | None = Field(None, max_length=1000)
    location: str | None = Field(None, max_length=255)

@router.patch("/me", response_model=UserResponse)
async def update_me(payload: ProfileUpdate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if current_user.role == "STUDENT" and not payload.phone:
        raise HTTPException(422, "Student phone number is required")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(current_user, key, value)
    await db.commit()
    return current_user

@router.get("/context")
async def auth_context(
    studio_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    rows = (await db.execute(select(OrganizationMember, Organization).join(Organization).where(
        OrganizationMember.user_id == current_user.id, OrganizationMember.role == current_user.role,
        OrganizationMember.is_active == True, Organization.deleted_at.is_(None), Organization.status == "ACTIVE"
    ).order_by(OrganizationMember.created_at))).all()
    selected = None
    if studio_id:
        for r in rows:
            if str(r[1].id) == studio_id or r[1].slug == studio_id:
                selected = r
                break
    if not selected and rows:
        selected = rows[0]
    return {
        "role": current_user.role,
        "organization_id": str(selected[1].id) if selected else None,
        "organization_name": selected[1].name if selected else None,
        "organization_slug": selected[1].slug if selected else None,
        "teacher_code": selected[0].teacher_code if selected and current_user.role == "TEACHER" else None,
        "organizations": [
            {
                "id": str(r[1].id),
                "name": r[1].name,
                "slug": r[1].slug,
                "teacher_code": r[0].teacher_code if current_user.role == "TEACHER" else None
            }
            for r in rows
        ]
    }

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str

@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    otp_code, record_id, msg = await AuthService.forgot_password(db, payload.email)
    return {
        "message": msg,
        "otp_record_id": record_id,
        "otp_preview": otp_code if settings.AUTH_SHOW_OTP_PREVIEW else None
    }

@router.post("/reset-password")
async def reset_password(payload: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    msg = await AuthService.reset_password(db, payload.email, payload.code, payload.new_password)
    return {"message": msg, "success": True}
