from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List

class Settings(BaseSettings):
    PROJECT_NAME: str = "ProgressRooms API"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"
    
    # Security
    SECRET_KEY: str = "dev-secret-key-change-in-production-progressrooms-2026"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7 # 7 days
    OTP_EXPIRE_MINUTES: int = 10
    
    # Database (Defaults to local PostgreSQL, falls back to SQLite for tests/offline)
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/progressrooms"
    SYNC_DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/progressrooms"
    
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
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
