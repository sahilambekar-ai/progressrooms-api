"""Context-specific encoding for email HTML, attributes and subject headers."""
from html import escape
from app.common.web_urls import validate_web_url
from app.core.config import settings


def email_text(value: object) -> str:
    return escape(str(value), quote=True)


def email_href(value: str) -> str:
    return escape(validate_web_url(value, allow_http_loopback=settings.is_local_environment), quote=True)


def email_subject(value: str) -> str:
    return ' '.join(''.join(c if ord(c) >= 32 and ord(c) != 127 else ' ' for c in value).split())


def first_name(value: str, fallback: str = 'Student') -> str:
    words = value.split() if value else []
    return words[0] if words else fallback
