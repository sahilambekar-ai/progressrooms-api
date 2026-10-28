import jwt
import secrets
from datetime import datetime, timedelta, timezone
import bcrypt
from app.core.config import settings

# Explicit Argon2id parameters meet OWASP's minimum (19 MiB, t=2, p=1).
from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError, InvalidHashError

password_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        if len(plain_password) > 128:
            return False
        if hashed_password.startswith("$argon2id$"):
            return password_hasher.verify(hashed_password, plain_password)
        if hashed_password.startswith(("$2a$", "$2b$", "$2y$")):
            # A legacy bcrypt hash cannot prove any suffix past byte 72.
            # Inputs at that boundary also require email recovery: an exact 72-byte
            # prefix cannot be distinguished from a previously truncated password.
            encoded = plain_password.encode("utf-8")
            return len(encoded) < 72 and bcrypt.checkpw(encoded, hashed_password.encode("utf-8"))
        return False
    except (VerificationError, InvalidHashError, ValueError, TypeError, UnicodeError):
        return False


def password_needs_rehash(hashed_password: str) -> bool:
    return not hashed_password.startswith("$argon2id$") or password_hasher.check_needs_rehash(hashed_password)


def get_password_hash(password: str) -> str:
    return password_hasher.hash(password)

def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    to_encode.setdefault("sv", 0)
    to_encode["jti"] = secrets.token_hex(16)
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None

def generate_otp(length: int = 6) -> str:
    """Generate a numeric OTP using a cryptographic random source."""
    return "".join(secrets.choice("0123456789") for _ in range(length))
