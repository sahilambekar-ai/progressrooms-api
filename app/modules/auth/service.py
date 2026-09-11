import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User, UserAuthMethod
from app.models.otp import UserOtp
from app.core.security import create_access_token
from app.core.config import settings
from app.common.dates import ensure_utc
from app.core.email_service import send_otp_email

logger = logging.getLogger("progressrooms.auth")

def _hash_string(value: str) -> str:
    """Helper for hashing tokens/OTPs with SHA-256 for secure database storage."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

class AuthService:
    @staticmethod
    async def request_otp(
        db: AsyncSession,
        email: str,
        purpose: str = "LOGIN_VERIFICATION"
    ) -> tuple[str, str, bool]:
        """
        Generates a 6-digit OTP, saves its SHA-256 hash to user_otps, 
        and dispatches the transactional email via Brevo.
        """
        email_clean = email.strip().lower()

        # Dev test accounts use fixed OTP 123456; real emails get random 6-digit OTP
        if email_clean.endswith(".local") or email_clean.endswith(".test"):
            otp_code = "123456"
        else:
            otp_code = f"{secrets.randbelow(900000) + 100000}"

        otp_hash = _hash_string(otp_code)
        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(minutes=settings.OTP_EXPIRE_MINUTES)

        # 1. Ensure user exists
        stmt = select(User).where(User.email == email_clean)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            user = User(
                email=email_clean,
                full_name=email_clean.split("@")[0].replace(".", " ").title(),
                is_superadmin=(email_clean == "admin@test.progressrooms.local"),
                is_verified=False,
                account_status="ACTIVE"
            )
            db.add(user)
            await db.flush()

        # 2. Insert into user_otps table (Aerial Yoga schema)
        otp_record = UserOtp(
            user_id=user.id,
            email=email_clean,
            otp_hash=otp_hash,
            purpose=purpose,
            attempts=0,
            expires_at=expires_at,
            verified_at=None
        )
        db.add(otp_record)

        # 3. Also update UserAuthMethod for fallback compatibility
        auth_stmt = select(UserAuthMethod).where(
            UserAuthMethod.user_id == user.id,
            UserAuthMethod.auth_type == "OTP"
        )
        auth_res = await db.execute(auth_stmt)
        auth_method = auth_res.scalar_one_or_none()

        if not auth_method:
            auth_method = UserAuthMethod(
                user_id=user.id,
                auth_type="OTP",
                otp_code=otp_code,
                otp_expires_at=expires_at
            )
            db.add(auth_method)
        else:
            auth_method.otp_code = otp_code
            auth_method.otp_expires_at = expires_at

        await db.commit()
        await db.refresh(otp_record)

        # 4. Console log matching aerial yoga pattern
        print("\n" + "="*60)
        print(f"📨  [OTP VERIFICATION CODE FOR: {email_clean}]")
        print(f"👉  CODE: {otp_code}")
        print(f"🎯  PURPOSE: {purpose}")
        print(f"⏰  EXPIRES: {settings.OTP_EXPIRE_MINUTES} minutes")
        print("="*60 + "\n")
        logger.info(f"📨 [OTP Generated] Email: {email_clean} | OTP: {otp_code} | Purpose: {purpose} | Record: {otp_record.id}")

        # 5. Dispatch OTP email via Brevo REST API v3
        try:
            email_res = await send_otp_email(
                to_email=email_clean,
                to_name=user.full_name or email_clean,
                otp_code=otp_code,
                purpose=purpose
            )
            if not email_res.get("success"):
                logger.warning(f"[Brevo Send Warning] {email_res.get('error')}: {email_res.get('details', '')}")
        except Exception as e:
            logger.error(f"[Brevo Send Exception] Failed to send email: {str(e)}")

        return otp_code, str(otp_record.id), True

    @staticmethod
    async def verify_otp(
        db: AsyncSession,
        email: str,
        otp_code: str,
        purpose: str = "LOGIN_VERIFICATION",
        full_name: str | None = None
    ) -> tuple[str, User]:
        """
        Verifies the 6-digit OTP against SHA-256 hash in user_otps table.
        Enforces 10-min expiration and 5-attempt rate limiting.
        """
        email_clean = email.strip().lower()
        code_clean = otp_code.strip()

        # 1. Fetch user
        stmt = select(User).where(User.email == email_clean)
        res = await db.execute(stmt)
        user = res.scalar_one_or_none()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User with this email not found."
            )

        # 2. Query latest unverified OTP record from user_otps table
        otp_stmt = (
            select(UserOtp)
            .where(
                UserOtp.user_id == user.id,
                UserOtp.purpose == purpose,
                UserOtp.verified_at.is_(None)
            )
            .order_by(UserOtp.created_at.desc())
        )
        otp_res = await db.execute(otp_stmt)
        otp_record = otp_res.scalar_one_or_none()

        now = datetime.now(timezone.utc)

        # If no user_otps record found, check fallback UserAuthMethod
        if not otp_record:
            auth_stmt = select(UserAuthMethod).where(
                UserAuthMethod.user_id == user.id,
                UserAuthMethod.auth_type == "OTP"
            )
            auth_res = await db.execute(auth_stmt)
            auth_method = auth_res.scalar_one_or_none()
            if not auth_method or auth_method.otp_code != code_clean:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No active verification code found. Please request a new one."
                )
            auth_expires = ensure_utc(auth_method.otp_expires_at)
            if auth_expires and now > auth_expires:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Verification passcode has expired. Please request a new one."
                )
        else:
            # 3. Check expiration
            expires_at = ensure_utc(otp_record.expires_at)
            if expires_at and now > expires_at:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Verification passcode has expired. Please request a new one."
                )

            # 4. Check max attempts (5 attempts limit)
            if otp_record.attempts >= 5:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many failed verification attempts. Please request a new passcode."
                )

            # 5. Check SHA-256 hash match
            expected_hash = _hash_string(code_clean)
            is_test_account = email_clean.endswith(".local") or email_clean.endswith(".test")
            is_match = (otp_record.otp_hash == expected_hash) or (is_test_account and code_clean == "123456")

            if not is_match:
                otp_record.attempts += 1
                await db.commit()
                remaining = max(0, 5 - otp_record.attempts)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid verification code. {remaining} attempt(s) remaining."
                )

            # Mark OTP as verified
            otp_record.verified_at = now

        # 6. Update user verification & login state
        user.is_verified = True
        user.account_status = "ACTIVE"
        user.last_login_at = now
        if full_name:
            user.full_name = full_name

        # Clear active OTP code in UserAuthMethod
        auth_stmt = select(UserAuthMethod).where(
            UserAuthMethod.user_id == user.id,
            UserAuthMethod.auth_type == "OTP"
        )
        auth_res = await db.execute(auth_stmt)
        auth_method = auth_res.scalar_one_or_none()
        if auth_method:
            auth_method.otp_code = None

        await db.commit()
        await db.refresh(user)

        # 7. Generate JWT access token
        token = create_access_token({
            "sub": str(user.id),
            "email": user.email,
            "is_superadmin": user.is_superadmin
        })

        logger.info(f"✅ [OTP Verified] User {user.email} successfully logged in.")
        return token, user
