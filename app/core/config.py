from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List

class Settings(BaseSettings):
    PROJECT_NAME: str = "Progress Rooms API"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"
    
    # Security
    SECRET_KEY: str = "dev-secret-key-change-in-production-progressrooms-2026"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7 # 7 days
    AUTH_SHOW_OTP_PREVIEW: bool = False
    # Local-only shortcut for seeded test accounts. Keep disabled in every
    # shared, staging, and production environment.
    DEV_TEST_LOGIN_ENABLED: bool = False
    CLASS_MEETING_BASE_URL: str = "https://meet.jit.si"
    OTP_EXPIRE_MINUTES: int = 10
    
    # Zoom platform app credentials stay on the server. Disabled unless configured.
    APP_ENV: str = "production"
    ZOOM_PROVIDER_MODE: str = "disabled"  # disabled, mock, live
    ZOOM_CLIENT_ID: str = ""
    ZOOM_CLIENT_SECRET: str = ""
    ZOOM_REDIRECT_URI: str = "http://localhost:3000/integrations/zoom/callback"
    ZOOM_WEBHOOK_SECRET: str = ""
    ZOOM_TOKEN_ENCRYPTION_KEY: str = ""  # Fernet key, separate from authentication secrets

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
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
