"""Validate web destinations before using them in email links or configuration."""
import ipaddress
import re
from urllib.parse import urlsplit


def validate_web_url(value: str, *, allow_http_loopback: bool = False, origin: bool = False) -> str:
    if not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or '\\' in value:
        raise ValueError('Expected a safe absolute web URL')
    try:
        parts = urlsplit(value)
        host = parts.hostname
        _ = parts.port  # Validate malformed/out-of-range ports too.
        if not host or parts.username is not None or parts.password is not None:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
            loopback = address.is_loopback
        except ValueError:
            ascii_host = host.encode('idna').decode('ascii')
            if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?', ascii_host):
                raise ValueError()
            loopback = host.lower().rstrip('.') == 'localhost'
        if parts.scheme != 'https' and not (parts.scheme == 'http' and allow_http_loopback and loopback):
            raise ValueError()
        if origin and (parts.path not in ('', '/') or parts.query or parts.fragment or loopback):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise ValueError('Expected an HTTPS web URL without credentials; HTTP is local-only') from None
    return value
