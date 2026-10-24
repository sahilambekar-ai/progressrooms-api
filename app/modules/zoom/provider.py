"""The mock and live adapters use the same authorization and meeting contracts."""
import hashlib
from urllib.parse import urlencode
import httpx
from fastapi import HTTPException
from cryptography.fernet import Fernet, InvalidToken
from app.core.config import settings
import json


class ProviderError(Exception):
    def __init__(self, message="Zoom is temporarily unavailable", *, invalid_grant=False, ambiguous=False):
        super().__init__(message)
        self.invalid_grant = invalid_grant
        self.ambiguous = ambiguous


def mode():
    value = settings.ZOOM_PROVIDER_MODE
    if value == "mock" and settings.APP_ENV not in {"development", "test"}:
        raise HTTPException(503, "Mock Zoom is disabled outside development and tests")
    if value not in {"mock", "live"}:
        raise HTTPException(503, "Zoom setup is awaiting platform configuration")
    if value == "live" and not all((settings.ZOOM_CLIENT_ID, settings.ZOOM_CLIENT_SECRET, settings.ZOOM_WEBHOOK_SECRET, settings.ZOOM_REDIRECT_URI.startswith("https://"))):
        raise HTTPException(503, "Zoom platform configuration is incomplete")
    cipher()  # Fail before creating attempts when encryption is not configured.
    return value


def cipher():
    try:
        return Fernet(settings.ZOOM_TOKEN_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError):
        raise HTTPException(503, "Zoom token encryption is not configured")


def seal(data):
    return cipher().encrypt(json.dumps(data).encode()).decode()


def unseal(value):
    try:
        return json.loads(cipher().decrypt(value.encode()))
    except (InvalidToken, ValueError, AttributeError):
        raise HTTPException(503, "Zoom credentials require recovery; contact the studio owner")


class ZoomProvider:
    def __init__(self, selected_mode=None):
        self.mode = mode()
        if selected_mode and selected_mode != self.mode:
            raise HTTPException(409, "This connection belongs to another Zoom environment; reconnect it")

    def authorize_url(self, state):
        if self.mode == "mock":
            return settings.ZOOM_REDIRECT_URI + "?" + urlencode({"state": state, "code": "mock_" + hashlib.sha256(state.encode()).hexdigest()})
        return "https://zoom.us/oauth/authorize?" + urlencode({"response_type": "code", "client_id": settings.ZOOM_CLIENT_ID, "redirect_uri": settings.ZOOM_REDIRECT_URI, "state": state})

    async def request(self, method, path, *, token=None, data=None, payload=None, oauth=False, ambiguous=False, allow_not_found=False):
        url = "https://zoom.us/oauth/" + path if oauth else "https://api.zoom.us/v2/" + path
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                result = await client.request(method, url, auth=(settings.ZOOM_CLIENT_ID, settings.ZOOM_CLIENT_SECRET) if oauth else None,
                    headers={"Authorization": "Bearer " + token} if token else {}, data=data, json=payload)
        except httpx.RequestError:
            raise ProviderError(ambiguous=ambiguous)
        if result.status_code == 404 and allow_not_found:
            return {}
        if result.status_code >= 400:
            raise ProviderError("Zoom authorization requires reconnecting" if result.status_code in {400, 401} and oauth else "Zoom rejected the operation; check account policies and permissions",
                                invalid_grant=result.status_code == 401 or (oauth and result.status_code == 400), ambiguous=ambiguous and result.status_code >= 500)
        return result.json() if result.content else {}

    async def exchange(self, code, state, member):
        if self.mode == "mock":
            if code != "mock_" + hashlib.sha256(state.encode()).hexdigest():
                raise ProviderError("Invalid development authorization code")
            identity = {"id": "mock-user-" + str(member.id), "account_id": "mock-account-" + str(member.organization_id),
                        "email": "teacher-" + str(member.id)[:8] + "@zoom.example.invalid", "display_name": "Development Zoom teacher"}
            return self.mock_tokens(identity["id"]), identity
        tokens = await self.request("POST", "token", oauth=True, data={"grant_type": "authorization_code", "code": code, "redirect_uri": settings.ZOOM_REDIRECT_URI})
        granted = set(tokens.get("scope", "").split())
        required = {"user:read:user": "user:read", "meeting:write:meeting": "meeting:write", "meeting:read:meeting": "meeting:read",
                    "meeting:read:list_meetings": "meeting:read", "meeting:update:meeting": "meeting:write", "meeting:delete:meeting": "meeting:write"}
        if any(scope not in granted and broad not in granted for scope, broad in required.items()):
            await self.revoke(tokens)
            raise ProviderError("Zoom app is missing required user/meeting permissions; update scopes and reconnect")
        identity = await self.request("GET", "users/me", token=tokens["access_token"])
        if not all(identity.get(k) for k in ("id", "account_id", "email")) or not tokens.get("refresh_token"):
            raise ProviderError("Zoom did not return a complete authorized identity")
        return tokens, identity

    def mock_tokens(self, user_id):
        import secrets
        return {"access_token": "mock-access-" + secrets.token_urlsafe(24), "refresh_token": "mock-refresh-" + secrets.token_urlsafe(24),
                "expires_in": 3600, "scope": "development-simulation", "mock_user": user_id}

    async def refresh(self, tokens):
        if self.mode == "mock":
            return self.mock_tokens(tokens.get("mock_user", "mock"))
        return await self.request("POST", "token", oauth=True, data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})

    async def revoke(self, tokens):
        if self.mode != "mock":
            await self.request("POST", "revoke", oauth=True, data={"token": tokens["access_token"]})

    async def create(self, token, payload, operation):
        payload = dict(payload, topic=payload["topic"][:150] + " [PR:" + operation + "]")
        if self.mode == "mock":
            meeting_id = "mock-" + operation
            return {"id": meeting_id, "join_url": settings.WEBSITE_PUBLIC_ORIGIN + "/integrations/zoom/mock-room?meeting=" + meeting_id,
                    "start_url": settings.WEBSITE_PUBLIC_ORIGIN + "/integrations/zoom/mock-room?host=1&meeting=" + meeting_id, "settings": payload["settings"]}
        result = await self.request("POST", "users/me/meetings", token=token, payload=payload, ambiguous=True)
        if not result.get("id") or not result.get("join_url"):
            raise ProviderError("Zoom returned an incomplete meeting response", ambiguous=True)
        return result

    async def meeting(self, token, meeting_id):
        if self.mode == "mock":
            return {"id": meeting_id, "start_url": settings.WEBSITE_PUBLIC_ORIGIN + "/integrations/zoom/mock-room?host=1&meeting=" + meeting_id}
        return await self.request("GET", "meetings/" + str(meeting_id), token=token)

    async def update(self, token, meeting_id, payload):
        if self.mode != "mock":
            await self.request("PATCH", "meetings/" + str(meeting_id), token=token, payload=payload)

    async def delete(self, token, meeting_id):
        if self.mode != "mock":
            # A second successful local cancellation does not repeat this call.
            await self.request("DELETE", "meetings/" + str(meeting_id), token=token, allow_not_found=True)

    async def reconcile(self, token, operation):
        if self.mode == "mock":
            return {"id": "mock-" + operation, "join_url": settings.WEBSITE_PUBLIC_ORIGIN + "/integrations/zoom/mock-room?meeting=mock-" + operation}
        page = ""
        for _ in range(100):
            data = await self.request("GET", "users/me/meetings?" + urlencode({"type": "scheduled", "page_size": 100, "next_page_token": page}), token=token)
            matches = [m for m in data.get("meetings", []) if "[PR:" + operation + "]" in m.get("topic", "")]
            if len(matches) > 1:
                raise ProviderError("Multiple provider meetings found; manual reconciliation is required")
            if matches:
                return await self.meeting(token, matches[0]["id"])
            page = data.get("next_page_token", "")
            if not page:
                return None
        raise ProviderError("Zoom meeting reconciliation exceeded the supported page limit")
