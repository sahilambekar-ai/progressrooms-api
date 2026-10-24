from app.common.phone import PhoneNumber
from pydantic import BaseModel, EmailStr, Field, StrictBool, field_validator, model_validator
from typing import Literal
import uuid

class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    phone: PhoneNumber | None = None
    whatsapp_number: PhoneNumber | None = None
    role: Literal["STUDIO", "STUDENT"] = "STUDIO"
    location: str | None = Field(default=None, max_length=255)
    studio_name: str | None = None
    selected_plan: Literal["starter", "core", "studio"] = "starter"
    billing_country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    privacy_accepted: StrictBool
    privacy_policy_version: Literal["2026-10-21", "2026-10-23"]

    @model_validator(mode="after")
    def require_student_phone(self):
        if self.role == "STUDENT" and not self.phone:
            raise ValueError("Student phone number is required")
        return self

    @field_validator("privacy_accepted")
    @classmethod
    def require_privacy_consent(cls, value):
        if value is not True:
            raise ValueError("Privacy Policy consent is required")
        return value

class RegisterResponse(BaseModel):
    message: str
    email: str
    is_verified: bool = False
    otp_record_id: str | None = None
    otp_preview: str | None = None

class LoginRequest(BaseModel):
    email: str
    password: str

class DevLoginRequest(BaseModel):
    role: str

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
    role: str
    phone: str | None = None
    whatsapp_number: str | None = None
    username: str | None = None
    address: str | None = None
    location: str | None = None
    billing_country: str | None = None
    avatar_url: str | None = None

    class Config:
        from_attributes = True


class DashboardPreferences(BaseModel):
    dashboard_font: Literal["lora", "manrope", "dm-sans", "ibm-plex", "source-sans", "source-serif"] = "ibm-plex"
