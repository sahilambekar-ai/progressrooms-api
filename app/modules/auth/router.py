from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.modules.auth.schemas import (
    RegisterRequest,
    RegisterResponse,
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
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user={
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "is_superadmin": user.is_superadmin,
            "is_verified": getattr(user, "is_verified", True),
            "account_status": getattr(user, "account_status", "ACTIVE")
        }
    )

@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user
