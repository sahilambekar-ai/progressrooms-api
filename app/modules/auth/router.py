from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.modules.auth.schemas import RequestOTPRequest, RequestOTPResponse, VerifyOTPRequest, TokenResponse, UserResponse
from app.modules.auth.service import AuthService
from app.modules.auth.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/request-otp", response_model=RequestOTPResponse)
@router.post("/otp/generate", response_model=RequestOTPResponse)
async def request_otp(payload: RequestOTPRequest, db: AsyncSession = Depends(get_db)):
    otp_code, record_id, ok = await AuthService.request_otp(db, payload.email, payload.purpose)
    return RequestOTPResponse(
        message=f"OTP successfully sent to {payload.email}",
        otp_record_id=record_id,
        otp_preview=otp_code
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
