from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Literal
from pydantic import field_validator, model_validator
from app.common.web_urls import validate_web_url
from uuid import UUID

class Settings(BaseSettings):
    PROJECT_NAME: str = "Progress Rooms API"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"
    
    # Security
    SECRET_KEY: str = "dev-secret-key-change-in-production-progressrooms-2026"
    ALGORITHM: Literal["HS256", "HS384", "HS512"] = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7 # 7 days
    AUTH_SHOW_OTP_PREVIEW: bool = False
    # Local-only shortcut for seeded test accounts. Keep disabled in every
    # shared, staging, and production environment.
    DEV_TEST_LOGIN_ENABLED: bool = False
    CLASS_MEETING_BASE_URL: str = "https://meet.jit.si"
    OTP_EXPIRE_MINUTES: int = 10
    
    FINANCIAL_DATA_ENCRYPTION_KEY: str = ""  # Independent Fernet key for shared bank writes

    # Zoom platform app credentials stay on the server. Disabled unless configured.
    APP_ENV: Literal["development", "test", "uat", "staging", "production"] = "production"
    ZOOM_PROVIDER_MODE: Literal["disabled", "mock", "live"] = "disabled"  # disabled, mock, live
    ZOOM_CLIENT_ID: str = ""
    ZOOM_CLIENT_SECRET: str = ""
    ZOOM_REDIRECT_URI: str = "http://localhost:3000/integrations/zoom/callback"
    ZOOM_WEBHOOK_SECRET: str = ""
    ZOOM_TOKEN_ENCRYPTION_KEY: str = ""  # Fernet key, separate from authentication secrets

    # Meeting notifications use Zoom Mail only. Billing/OTP keep Brevo.
    MEETING_EMAIL_ORGANIZATION_ID: UUID | None = None  # Optional single-tenant validation worker
    MEETING_EMAIL_WORKER_INTERVAL_SECONDS: int = 15
    MEETING_EMAIL_CONCURRENCY: int = 4
    MEETING_EMAIL_BATCH_SIZE: int = 100

    # Database (Defaults to local PostgreSQL, falls back to SQLite for tests/offline)
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/progressrooms"
    SYNC_DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/progressrooms"
    
    # Absolute class links also work from downloaded standalone HTML files.
    WEBSITE_PUBLIC_ORIGIN: str = "http://localhost:3000"
    WEBSITE_MEDIA_DIR: str = "uploads/websites"
    WEBSITE_EXPORT_DIR: str = "generated-websites"

    # CORS
    BACKEND_CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://progressrooms.com",
    ]
    
    # Razorpay
    RAZORPAY_KEY_ID: str = "rzp_test_mock_progressrooms"
    RAZORPAY_KEY_SECRET: str = "rzp_secret_mock_progressrooms"
    RAZORPAY_WEBHOOK_SECRET: str = "rzp_webhook_secret_mock"
    # SaaS billing is enabled only after the merchant explicitly configures fees.
    # Basis points: 100 = 1%. None is unconfigured; zero is an explicit zero fee.
    SAAS_GST_BPS: int | None = 1800
    SAAS_PLATFORM_FEE_BPS: int | None = 0
    SAAS_PLATFORM_FEE_PAISE: int | None = 0
    SAAS_GST_ON_PLATFORM_FEE: bool | None = False
    SAAS_RAZORPAY_WEBHOOK_SECRET: str = ""
    SAAS_ACCESS_ENFORCEMENT_ENABLED: bool = False
    
    # Brevo Mail Service Configuration
    BREVO_API_KEY: str = ""
    BREVO_SENDER_EMAIL: str = "sahilambekar.dev@gmail.com"
    BREVO_SENDER_NAME: str = "Progress Rooms Virtual Studio"
    BREVO_ENABLED: bool = True
    
    @field_validator("FINANCIAL_DATA_ENCRYPTION_KEY")
    @classmethod
    def validate_financial_key(cls, value):
        if value:
            from cryptography.fernet import Fernet
            try:
                Fernet(value.encode())
            except (ValueError, TypeError):
                raise ValueError("Financial data encryption key must be a valid Fernet key")
        return value

    @field_validator("APP_ENV", mode="before")
    @classmethod
    def normalize_environment(cls, value):
        value = str(value).strip().lower()
        return {"dev": "development", "local": "development", "testing": "test",
                "prod": "production", "stage": "staging"}.get(value, value)

    @property
    def is_local_environment(self) -> bool:
        return self.APP_ENV in {"development", "test"}

    @property
    def development_login_enabled(self) -> bool:
        return self.is_local_environment and self.DEV_TEST_LOGIN_ENABLED

    @property
    def otp_preview_enabled(self) -> bool:
        return self.is_local_environment and self.AUTH_SHOW_OTP_PREVIEW

    @model_validator(mode="after")
    def enforce_shared_environment_security(self):
        if self.is_local_environment:
            return self
        key = self.SECRET_KEY
        minimum = {"HS256": 32, "HS384": 48, "HS512": 64}[self.ALGORITHM]
        placeholders = ("dev-secret", "change-in-production", "changeme", "change-me",
                        "your-secret", "generate_a_unique", "replace-me", "ci-only", "test-secret", "example-secret")
        if len(key.encode()) < minimum or len(set(key)) < 12 or any(p in key.lower() for p in placeholders):
            raise ValueError("Shared environments require a unique randomly generated SECRET_KEY of sufficient length")
        if self.DEV_TEST_LOGIN_ENABLED or self.AUTH_SHOW_OTP_PREVIEW:
            raise ValueError("Developer login and OTP previews are forbidden in shared environments")
        if self.ZOOM_PROVIDER_MODE == "mock":
            raise ValueError("Mock Zoom is forbidden in shared environments")
        validate_web_url(self.WEBSITE_PUBLIC_ORIGIN, origin=True)
        if not self.BACKEND_CORS_ORIGINS:
            raise ValueError("Shared environments require explicit HTTPS CORS origins")
        for origin in self.BACKEND_CORS_ORIGINS:
            validate_web_url(origin, origin=True)
        return self

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

settings = Settings()
