"""Versioned bank-field encryption; API views only return masked values."""

import base64
import hashlib
import re

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from app.core.config import settings

PREFIX = "fin1:"


def cipher():
    key = settings.FINANCIAL_DATA_ENCRYPTION_KEY
    if not key:
        if not settings.is_local_environment:
            raise HTTPException(503, "Financial data encryption is not configured")
        key = base64.urlsafe_b64encode(
            hashlib.sha256(
                ("financial-data-local:" + settings.SECRET_KEY).encode()
            ).digest()
        ).decode()
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise HTTPException(503, "Financial data encryption is not configured")


def encrypt_account(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9 -]{1,100}", value):
        raise HTTPException(
            422,
            "Use at most 100 letters, digits, spaces or hyphens for the bank account",
        )
    return PREFIX + cipher().encrypt(value.encode()).decode()


def decrypt_account(value: str) -> str:
    if not value.startswith(PREFIX):
        return value  # Legacy reads are confined to masking or explicit migration.
    try:
        return cipher().decrypt(value[len(PREFIX) :].encode()).decode()
    except (InvalidToken, UnicodeError):
        raise HTTPException(503, "Financial data requires key recovery")


def mask_account(value: str | None) -> str | None:
    if not value:
        return None
    try:
        plain = decrypt_account(value)
    except HTTPException:
        return "•••• (key recovery required)"
    return "••••" + (plain[-4:] if len(plain) > 4 else "")
