from pydantic import BaseModel, EmailStr
import uuid

class RegisterRequest(BaseModel):
    full_name: str
    email: str
    password: str | None = None
    phone: str | None = None
    role: str = "STUDIO" # "STUDIO" or "STUDENT"
    studio_name: str | None = None

class RegisterResponse(BaseModel):
    message: str
    email: str
    is_verified: bool = False
    otp_record_id: str | None = None
    otp_preview: str | None = None

class LoginRequest(BaseModel):
    email: str
    password: str

class LoginResponse(BaseModel):
    access_token: str | None = None
    token_type: str = "bearer"
    user: dict | None = None
    is_verified: bool = True
    requires_activation: bool = False
    message: str | None = None
    otp_preview: str | None = None

class RequestOTPRequest(BaseModel):
    email: str
    purpose: str = "LOGIN_VERIFICATION"

class RequestOTPResponse(BaseModel):
    message: str
    otp_record_id: str | None = None
    otp_preview: str | None = None # Included in development/test environment for easy access
    is_verified: bool = True
    requires_activation: bool = False

class VerifyOTPRequest(BaseModel):
    email: str
    otp: str | None = None
    otp_code: str | None = None
    purpose: str = "LOGIN_VERIFICATION"
    full_name: str | None = None

    @property
    def code(self) -> str:
        return self.otp or self.otp_code or ""

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
