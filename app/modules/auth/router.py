from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.modules.auth.schemas import RequestOTPRequest, RequestOTPResponse, VerifyOTPRequest, TokenResponse, UserResponse
from app.modules.auth.service import AuthService
from app.modules.auth.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/request-otp", response_model=RequestOTPResponse)
async def request_otp(payload: RequestOTPRequest, db: AsyncSession = Depends(get_db)):
    otp_code, ok = await AuthService.request_otp(db, payload.email)
    return RequestOTPResponse(
        message=f"OTP successfully sent to {payload.email}",
        otp_preview=otp_code
    )

@router.post("/verify-otp", response_model=TokenResponse)
async def verify_otp(payload: VerifyOTPRequest, db: AsyncSession = Depends(get_db)):
    result = await AuthService.verify_otp(db, payload.email, payload.otp, payload.full_name)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OTP code"
        )
    token, user = result
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user={
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "is_superadmin": user.is_superadmin
        }
    )

@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user
