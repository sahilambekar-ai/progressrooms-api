from pydantic import BaseModel, EmailStr
import uuid

class RequestOTPRequest(BaseModel):
    email: EmailStr

class RequestOTPResponse(BaseModel):
    message: str
    otp_preview: str | None = None # Included in development/test environment for easy access

class VerifyOTPRequest(BaseModel):
    email: EmailStr
    otp: str
    full_name: str | None = None

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict

class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_superadmin: bool
    avatar_url: str | None = None

    class Config:
        from_attributes = True
