import hashlib
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from app.models.otp import UserOtp
from app.models.organization import Organization, OrganizationMember, OrganizationStudent, OrganizationSetting
from app.core.security import create_access_token, verify_password, get_password_hash
from app.core.config import settings
from app.common.dates import ensure_utc
from app.core.email_service import send_otp_email

logger = logging.getLogger("progressrooms.auth")

def _hash_string(value: str) -> str:
    """Helper for hashing tokens/OTPs with SHA-256 for secure database storage."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

class AuthService:
    @staticmethod
    async def register_user(
        db: AsyncSession,
        full_name: str,
        email: str,
        password: str | None = None,
        phone: str | None = None,
        whatsapp_number: str | None = None,
        role: str = "STUDIO",
        studio_name: str | None = None,
        location: str | None = None,
        privacy_policy_version: str | None = None
    ) -> tuple[str, str, str]:
        """
        Creates a new unverified user (and studio organization if role is studio),
        generates an email verification OTP, and dispatches it via Brevo.
        """
        if role not in {"STUDIO", "STUDENT"}:
            raise HTTPException(400, "Only studios and students can register")
        if not password or len(password) < 8 or not full_name.strip():
            raise HTTPException(400, "Name and a password of at least 8 characters are required")
        if role == "STUDENT" and (not phone or not phone.strip() or not location or not location.strip()):
            raise HTTPException(400, "Phone and location are required")
        email_clean = email.strip().lower()
        stmt = select(User).where(User.email == email_clean)
        res = await db.execute(stmt)
        user = res.scalar_one_or_none()

        if user and user.is_verified:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An account with this email already exists. Sign in or request a verification code."
            )

        pwd_hash = get_password_hash(password) if password else None

        if not user:
            user = User(
                email=email_clean,
                full_name=full_name.strip(),
                phone=phone.strip() if phone else None,
                whatsapp_number=whatsapp_number,
                password_hash=pwd_hash,
                is_superadmin=False,
                role=role,
                location=location,
                is_verified=False,
                account_status="PENDING_VERIFICATION"
            )
            db.add(user)
            await db.flush()
        else:
            user.full_name = full_name.strip()
            user.whatsapp_number = whatsapp_number
            if phone and phone.strip():
                user.phone = phone.strip()
            if pwd_hash:
                user.password_hash = pwd_hash
            if location:
                user.location = location
            user.role = role
            user.account_status = "PENDING_VERIFICATION"

        # Setup Studio Organization if Studio/Instructor role
        if role == "STUDIO":
            mem_stmt = select(OrganizationMember, Organization).join(Organization).where(OrganizationMember.user_id == user.id, OrganizationMember.is_active == True)
            existing_mem_row = (await db.execute(mem_stmt)).first()
            if not existing_mem_row:
                org_display_name = (studio_name.strip() if studio_name else f"{full_name.strip()} Sanctuary")
                base_slug = re.sub(r'[^a-z0-9]+', '-', org_display_name.lower()).strip('-') or "sanctuary"
                slug = f"{base_slug}-{uuid.uuid4().hex[:6]}"
                new_org = Organization(
                    name=org_display_name,
                    slug=slug,
                    timezone="Asia/Kolkata",
                    currency="INR",
                    status="ACTIVE"
                )
                db.add(new_org)
                await db.flush()

                new_member = OrganizationMember(
                    organization_id=new_org.id,
                    user_id=user.id,
                    role="STUDIO",
                    title="Studio Founder & Lead Guide",
                    bio="Lead instructor and sanctuary director.",
                    is_active=True
                )
                db.add(new_member)

                new_settings = OrganizationSetting(
                    organization_id=new_org.id,
                    phone=phone.strip() if phone else None,
                    support_phone=phone.strip() if phone else None,
                    support_email=email_clean,
                    account_completed=False,
                    completion_step=1
                )
                db.add(new_settings)
            else:
                existing_mem, existing_org = existing_mem_row
                if studio_name and studio_name.strip():
                    existing_org.name = studio_name.strip()
        # Generate Email Verification OTP
        otp_code = f"{secrets.randbelow(900000) + 100000}"

        otp_hash = _hash_string(otp_code)
        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(minutes=settings.OTP_EXPIRE_MINUTES)

        from sqlalchemy import update
        await db.execute(update(UserOtp).where(UserOtp.user_id == user.id, UserOtp.verified_at.is_(None)).values(verified_at=now_utc))
        otp_record = UserOtp(
            user_id=user.id,
            email=email_clean,
            otp_hash=otp_hash,
            purpose="EMAIL_VERIFICATION",
            attempts=0,
            expires_at=expires_at,
            verified_at=None
        )
        db.add(otp_record)
        if privacy_policy_version:
            from app.models.audit import AuditLog
            db.add(AuditLog(
                user_id=user.id, entity_type="privacy_consent", entity_id=user.id,
                action="PRIVACY_POLICY_ACCEPTED",
                new_data={"policy_version": privacy_policy_version, "audience": role,
                          "accepted": True, "source": "registration"},
            ))
        await db.commit()
        await db.refresh(otp_record)

        try:
            await send_otp_email(
                to_email=email_clean,
                to_name=user.full_name or email_clean,
                otp_code=otp_code,
                purpose="EMAIL_VERIFICATION"
            )
        except Exception as e:
            logger.error(f"[Brevo Send Error] {str(e)}")

        msg = f"Registration successful! Please log in with the OTP sent to {email_clean} to verify and activate your account."
        return otp_code, str(otp_record.id), msg

    @staticmethod
    async def request_otp(
        db: AsyncSession,
        email: str,
        purpose: str = "LOGIN_VERIFICATION"
    ) -> tuple[str, str, bool, bool, str]:
        """
        Generates an OTP for sign in. Checks if user is verified.
        If unverified, flags requires_activation=True and returns appropriate message.
        """
        email_clean = email.strip().lower()

        otp_code = f"{secrets.randbelow(900000) + 100000}"

        otp_hash = _hash_string(otp_code)
        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(minutes=settings.OTP_EXPIRE_MINUTES)

        stmt = select(User).where(User.email == email_clean)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()

        is_verified = True
        requires_activation = False

        if not user:
            raise HTTPException(404, "Account not found. Please register first.")
        elif not getattr(user, "is_verified", False) or user.account_status == "PENDING_VERIFICATION":
            is_verified = False
            requires_activation = True
            msg = f"Your account is not verified yet. An activation OTP has been dispatched to {email_clean} to activate your account."
        else:
            is_verified = True
            requires_activation = False
            msg = f"Login OTP successfully sent to {email_clean}."

        from sqlalchemy import update
        await db.execute(update(UserOtp).where(UserOtp.user_id == user.id, UserOtp.verified_at.is_(None)).values(verified_at=now_utc))
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

        # user_otps is the verification source; do not mirror plaintext codes.
        await db.commit()
        await db.refresh(otp_record)

        try:
            await send_otp_email(
                to_email=email_clean,
                to_name=user.full_name or email_clean,
                otp_code=otp_code,
                purpose="EMAIL_VERIFICATION" if requires_activation else purpose
            )
        except Exception as e:
            logger.error(f"[Brevo Send Error] {str(e)}")

        return otp_code, str(otp_record.id), is_verified, requires_activation, msg

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
        Activates unverified accounts upon success.
        """
        email_clean = email.strip().lower()
        code_clean = otp_code.strip()

        stmt = select(User).where(User.email == email_clean)
        res = await db.execute(stmt)
        user = res.scalar_one_or_none()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User with this email not found."
            )

        # Allow verifying either LOGIN_VERIFICATION or EMAIL_VERIFICATION
        otp_stmt = (
            select(UserOtp)
            .where(
                UserOtp.user_id == user.id,
                UserOtp.verified_at.is_(None)
            )
            .order_by(UserOtp.created_at.desc())
            .limit(1)
        )
        otp_res = await db.execute(otp_stmt)
        otp_record = otp_res.scalars().first()

        now = datetime.now(timezone.utc)

        if not otp_record:
            raise HTTPException(400, "No active verification code. Request a new code.")
        else:
            expires_at = ensure_utc(otp_record.expires_at)
            if expires_at and now > expires_at:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Verification passcode has expired. Please request a new one."
                )

            if otp_record.attempts >= 5:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many failed verification attempts. Please request a new passcode."
                )

            expected_hash = _hash_string(code_clean)
            is_match = secrets.compare_digest(otp_record.otp_hash, expected_hash)

            if not is_match:
                otp_record.attempts += 1
                await db.commit()
                remaining = max(0, 5 - otp_record.attempts)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid verification code. {remaining} attempt(s) remaining."
                )

            otp_record.verified_at = now

        if not user.is_active or user.deleted_at or user.account_status in {"SUSPENDED", "DISABLED"}:
            raise HTTPException(403, "Account access is disabled")

        # Activate user account
        user.is_verified = True
        user.account_status = "ACTIVE"
        user.last_login_at = now
        if full_name:
            user.full_name = full_name

        await db.commit()
        await db.refresh(user)

        token = create_access_token({
            "sub": str(user.id),
            "email": user.email,
            "is_superadmin": user.is_superadmin, "role": user.role
        })

        logger.info(f"✅ [Account Verified & Logged In] User: {user.email} (Status: {user.account_status})")
        return token, user

    @staticmethod
    async def login_with_password(db: AsyncSession, email: str, password: str) -> dict:
        """
        Authenticates a user via email and password.
        If verified -> returns JWT access token and user payload.
        If NOT verified -> triggers activation OTP email and returns requires_activation=True.
        """
        email_clean = email.strip().lower()
        stmt = select(User).where(User.email == email_clean)
        res = await db.execute(stmt)
        user = res.scalar_one_or_none()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password."
            )

        # Password credentials have one authoritative location.
        if not user.password_hash or not verify_password(password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password."
            )

        if not user.is_active or user.deleted_at or user.account_status in ["SUSPENDED", "DISABLED"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Account is {user.account_status}. Please contact support."
            )

        # Check if user is verified
        if not user.is_verified or user.account_status == "PENDING_VERIFICATION":
            otp_code, record_id, _, _, msg = await AuthService.request_otp(
                db=db,
                email=user.email,
                purpose="LOGIN_VERIFICATION"
            )
            is_dev = settings.AUTH_SHOW_OTP_PREVIEW
            return {
                "access_token": None,
                "token_type": "bearer",
                "user": None,
                "is_verified": False,
                "requires_activation": True,
                "message": "Account is not verified yet. Please enter the OTP sent to your email to activate and verify your account.",
                "otp_preview": otp_code if is_dev else None
            }

        # User is verified!
        user.last_login_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(user)

        user_role = user.role
        org_id = None
        org_name = None
        org_slug = None
        account_completed = False
        completion_step = 1
        completion_percentage = 0
        zoom_connected = False
        teacher_code = None

        mem_stmt = select(OrganizationMember, Organization).join(
            Organization, Organization.id == OrganizationMember.organization_id
        ).where(OrganizationMember.user_id == user.id, OrganizationMember.is_active == True)
        mem_res = await db.execute(mem_stmt)
        mem_row = mem_res.first()
        if user.role == "TEACHER" and not mem_row:
            raise HTTPException(403, "Teacher access has been deactivated")
        if mem_row:
            member, org = mem_row
            teacher_code = member.teacher_code if user.role == "TEACHER" else None
            user_role = user.role
            org_id = str(org.id)
            org_name = org.name
            org_slug = org.slug

            set_stmt = select(OrganizationSetting).where(OrganizationSetting.organization_id == org.id)
            org_settings = (await db.execute(set_stmt)).scalar_one_or_none()
            if org_settings:
                account_completed = org_settings.account_completed
                completion_step = org_settings.completion_step
                completion_percentage = org_settings.completion_percentage
                zoom_connected = org_settings.zoom_connected
            else:
                new_settings = OrganizationSetting(
                    organization_id=org.id,
                    phone=user.phone,
                    support_phone=user.phone,
                    support_email=user.email,
                    account_completed=False,
                    completion_step=1,
                    completion_percentage=0
                )
                db.add(new_settings)
                await db.commit()

        token = create_access_token({
            "sub": str(user.id),
            "email": user.email,
            "role": user_role,
            "is_superadmin": user.is_superadmin, "role": user.role
        })

        return {
            "access_token": token,
            "token_type": "bearer",
            "is_verified": True,
            "requires_activation": False,
            "message": "Logged in successfully.",
            "user": {
                "id": str(user.id),
                "email": user.email,
                "full_name": user.full_name,
                "is_superadmin": user.is_superadmin,
                "role": user_role,
                "organization_id": org_id,
                "organization_name": org_name,
                "organization_slug": org_slug,
                "is_verified": True,
                "account_status": user.account_status,
                "account_completed": account_completed,
                "completion_step": completion_step,
                "completion_percentage": completion_percentage,
                "zoom_connected": zoom_connected,
                "teacher_code": teacher_code,
            }
        }

    @staticmethod
    async def forgot_password(db: AsyncSession, email: str) -> tuple[str, str, str]:
        email_clean = email.strip().lower()
        stmt = select(User).where(User.email == email_clean)
        res = await db.execute(stmt)
        user = res.scalar_one_or_none()

        if not user:
            return "", "", "If an account exists with this email, a password reset passcode has been sent."

        otp_code = f"{secrets.randbelow(900000) + 100000}"
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=10)

        otp_record = UserOtp(
            user_id=user.id,
            email=email_clean,
            otp_hash=_hash_string(otp_code),
            purpose="PASSWORD_RESET",
            expires_at=expires_at,
            attempts=0
        )
        db.add(otp_record)
        await db.commit()
        await db.refresh(otp_record)

        try:
            await send_otp_email(
                to_email=email_clean,
                to_name=user.full_name or email_clean,
                otp_code=otp_code,
                purpose="PASSWORD_RESET"
            )
        except Exception as e:
            logger.error(f"[Brevo Send Error] {str(e)}")

        return otp_code, str(otp_record.id), "Password reset passcode sent to your email."

    @staticmethod
    async def reset_password(db: AsyncSession, email: str, otp_code: str, new_password: str) -> str:
        email_clean = email.strip().lower()
        code_clean = otp_code.strip()

        if len(new_password) < 6:
            raise HTTPException(422, "Password must be at least 6 characters.")

        stmt = select(User).where(User.email == email_clean)
        res = await db.execute(stmt)
        user = res.scalar_one_or_none()
        if not user:
            raise HTTPException(400, "Invalid email or verification code.")

        otp_stmt = (
            select(UserOtp)
            .where(
                UserOtp.user_id == user.id,
                UserOtp.purpose == "PASSWORD_RESET",
                UserOtp.verified_at.is_(None)
            )
            .order_by(UserOtp.created_at.desc())
            .limit(1)
        )
        otp_record = (await db.execute(otp_stmt)).scalars().first()
        if not otp_record:
            raise HTTPException(400, "No active password reset request. Please request a new code.")

        now = datetime.now(timezone.utc)
        expires_at = ensure_utc(otp_record.expires_at)
        if expires_at and now > expires_at:
            raise HTTPException(400, "Password reset code has expired. Please request a new one.")

        if otp_record.attempts >= 5:
            raise HTTPException(429, "Too many failed attempts. Please request a new code.")

        expected_hash = _hash_string(code_clean)
        if not secrets.compare_digest(otp_record.otp_hash, expected_hash):
            otp_record.attempts += 1
            await db.commit()
            remaining = max(0, 5 - otp_record.attempts)
            raise HTTPException(400, f"Invalid verification code. {remaining} attempt(s) remaining.")

        otp_record.verified_at = now

        new_hash = get_password_hash(new_password)
        user.password_hash = new_hash

        user.is_verified = True
        user.account_status = "ACTIVE"
        await db.commit()

        return "Password has been reset successfully. You can now log in."
