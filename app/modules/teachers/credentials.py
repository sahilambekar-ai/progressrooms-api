"""Encrypted recoverable credentials for Studio-managed teachers only."""
import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from app.core.config import settings
from app.core.security import verify_password

def reconfirm(actor, password):
    if not actor.password_hash or not verify_password(password, actor.password_hash):
        raise HTTPException(403, "Studio password is incorrect")

def cipher():
    key = hashlib.sha256(("teacher-credentials:" + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))

def seal(password):
    return cipher().encrypt(password.encode()).decode()

def reveal(user):
    if not user.managed_password_enc:
        raise HTTPException(409, "Create a new password first; the existing password cannot be recovered")
    try:
        password = cipher().decrypt(user.managed_password_enc.encode()).decode()
    except InvalidToken:
        raise HTTPException(409, "Create a new password; the saved credential needs recovery")
    if not verify_password(password, user.password_hash):
        raise HTTPException(409, "Password changed outside Studio; create a new password first")
    return password
