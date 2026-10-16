import json
import logging
import uuid
import secrets
from datetime import datetime
from typing import Optional, Tuple
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import OrganizationIntegration
from app.models.organization import Organization, OrganizationSetting
from app.models.schedule import ClassOccurrence

logger = logging.getLogger(__name__)

class ZoomAuthError(Exception):
    def __init__(self, message: str, status_code: int = 400, details: dict = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}

class ZoomService:
    OAUTH_TOKEN_URL = "https://zoom.us/oauth/token"
    API_BASE_URL = "https://api.zoom.us/v2"

    @classmethod
    async def verify_and_obtain_token(cls, account_id: str, client_id: str, client_secret: str) -> dict:
        """
        Calls Zoom Server-to-Server OAuth endpoint to verify credentials and obtain an access token.
        Raises ZoomAuthError if credentials are invalid or provider is unreachable.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(
                    cls.OAUTH_TOKEN_URL,
                    params={
                        "grant_type": "account_credentials",
                        "account_id": account_id,
                    },
                    auth=(client_id, client_secret),
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
            except httpx.RequestError as exc:
                raise ZoomAuthError(f"Zoom authentication service unreachable: {str(exc)}", status_code=503)

            if response.status_code != 200:
                try:
                    err_json = response.json()
                    err_reason = err_json.get("error_description") or err_json.get("message") or response.text
                except Exception:
                    err_reason = response.text
                raise ZoomAuthError(
                    f"Zoom verification failed (HTTP {response.status_code}): {err_reason}",
                    status_code=400,
                    details={"zoom_response": response.text}
                )

            data = response.json()
            return {
                "access_token": data["access_token"],
                "token_type": data.get("token_type", "bearer"),
                "expires_in": data.get("expires_in", 3600),
            }

    @classmethod
    async def create_meeting(
        cls,
        access_token: str,
        topic: str,
        start_time: datetime,
        duration_minutes: int,
        timezone_str: str = "Asia/Kolkata",
        waiting_room: bool = True,
        host_video: bool = True,
        host_email: str = "me"
    ) -> dict:
        """
        Creates a scheduled Zoom meeting via real Zoom API.
        """
        url = f"{cls.API_BASE_URL}/users/{host_email}/meetings"
        payload = {
            "topic": topic,
            "type": 2, # Scheduled meeting
            "start_time": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration": max(15, duration_minutes),
            "timezone": timezone_str,
            "settings": {
                "waiting_room": waiting_room,
                "host_video": host_video,
                "participant_video": False,
                "join_before_host": False,
                "mute_upon_entry": True
            }
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    }
                )
            except httpx.RequestError as exc:
                raise ZoomAuthError(f"Zoom API request failed: {str(exc)}", status_code=503)

            if response.status_code not in (200, 201):
                try:
                    err_json = response.json()
                    err_msg = err_json.get("message") or response.text
                except Exception:
                    err_msg = response.text
                raise ZoomAuthError(f"Failed to create Zoom meeting (HTTP {response.status_code}): {err_msg}", status_code=400)

            data = response.json()
            return {
                "meeting_id": str(data["id"]),
                "passcode": data.get("password", ""),
                "join_url": data["join_url"],
                "start_url": data["start_url"],
            }

    @classmethod
    async def update_meeting(
        cls,
        access_token: str,
        meeting_id: str,
        start_time: datetime,
        duration_minutes: int,
        topic: Optional[str] = None
    ) -> bool:
        """Updates a scheduled Zoom meeting start time/duration."""
        url = f"{cls.API_BASE_URL}/meetings/{meeting_id}"
        payload = {
            "start_time": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration": max(15, duration_minutes),
        }
        if topic:
            payload["topic"] = topic

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.patch(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    }
                )
                return response.status_code == 204
            except Exception as e:
                logger.warning(f"Could not update Zoom meeting {meeting_id}: {e}")
                return False

    @classmethod
    async def delete_meeting(cls, access_token: str, meeting_id: str) -> bool:
        """Deletes a Zoom meeting."""
        url = f"{cls.API_BASE_URL}/meetings/{meeting_id}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.delete(
                    url,
                    headers={"Authorization": f"Bearer {access_token}"}
                )
                return response.status_code == 204
            except Exception as e:
                logger.warning(f"Could not delete Zoom meeting {meeting_id}: {e}")
                return False


async def get_or_create_occurrence_meeting(
    db: AsyncSession,
    occurrence: ClassOccurrence,
    org: Organization,
    is_host: bool = False
) -> dict:
    """
    Returns the persisted meeting for a ClassOccurrence.
    If not already provisioned, provisions exactly ONE provider-backed meeting,
    persists its join_url, host_url, and provider_meeting_id, and returns host/attendee access.
    Survives retries without creating duplicate rooms.
    Never invents fictitious meeting room fallbacks.
    """
    # 1. Idempotency: Return already persisted meeting if present
    if occurrence.meeting_url:
        target_url = occurrence.host_url if (is_host and occurrence.host_url) else occurrence.meeting_url
        return {
            "join_url": occurrence.meeting_url,
            "host_url": occurrence.host_url or occurrence.meeting_url,
            "target_url": target_url,
            "meeting_id": occurrence.provider_meeting_id,
            "provider": occurrence.meeting_provider or "ZOOM",
            "is_simulated": (occurrence.meeting_provider == "DEV_SIMULATION")
        }

    # 2. Check organization Zoom integration
    stmt = select(OrganizationIntegration).where(
        OrganizationIntegration.organization_id == org.id,
        OrganizationIntegration.provider == "ZOOM",
        OrganizationIntegration.is_active.is_(True)
    )
    integration = (await db.execute(stmt)).scalar_one_or_none()

    set_stmt = select(OrganizationSetting).where(OrganizationSetting.organization_id == org.id)
    settings = (await db.execute(set_stmt)).scalar_one_or_none()

    if not integration or not settings or not settings.zoom_connected:
        raise ZoomAuthError(
            "Live meeting room is not provisioned. Studio has not connected a verified Zoom account.",
            status_code=503,
            details={"configured": False}
        )

    cred_data = {}
    if integration.credentials_enc:
        try:
            cred_data = json.loads(integration.credentials_enc)
        except Exception:
            pass

    is_simulated = cred_data.get("is_simulated", False) or cred_data.get("mode") == "DEV_SIMULATION"

    if is_simulated:
        # Development simulation explicitly labeled
        sim_id = f"sim_{secrets.randbelow(900_000_000) + 100_000_000}"
        sim_pass = secrets.token_urlsafe(6)
        join_url = f"https://dev-simulation.zoom.us/j/{sim_id}?pwd={sim_pass}"
        host_url = f"https://dev-simulation.zoom.us/s/{sim_id}?zak=sim_zak_{secrets.token_hex(8)}"

        occurrence.meeting_url = join_url
        occurrence.host_url = host_url
        occurrence.meeting_provider = "DEV_SIMULATION"
        occurrence.provider_meeting_id = sim_id
        await db.commit()
        await db.refresh(occurrence)

        target_url = host_url if is_host else join_url
        return {
            "join_url": join_url,
            "host_url": host_url,
            "target_url": target_url,
            "meeting_id": sim_id,
            "provider": "DEV_SIMULATION",
            "is_simulated": True
        }

    # Real Zoom provider provisioning
    account_id = cred_data.get("account_id")
    client_id = cred_data.get("client_id")
    client_secret = cred_data.get("client_secret")

    if not (account_id and client_id and client_secret):
        raise ZoomAuthError(
            "Zoom credentials incomplete. Server-to-Server OAuth Account ID, Client ID, and Client Secret are required.",
            status_code=503
        )

    # Obtain real token
    token_res = await ZoomService.verify_and_obtain_token(account_id, client_id, client_secret)
    access_token = token_res["access_token"]

    duration = int((occurrence.actual_end_at - occurrence.actual_start_at).total_seconds() / 60)
    topic = f"{occurrence.session.name if occurrence.session else 'Live Class'} — ProgressRooms"

    meeting_res = await ZoomService.create_meeting(
        access_token=access_token,
        topic=topic,
        start_time=occurrence.actual_start_at,
        duration_minutes=duration,
        timezone_str=org.timezone or "Asia/Kolkata",
        waiting_room=settings.zoom_waiting_room if settings else True,
        host_video=settings.zoom_host_video if settings else True,
        host_email=settings.zoom_account_email or "me"
    )

    occurrence.meeting_url = meeting_res["join_url"]
    occurrence.host_url = meeting_res["start_url"]
    occurrence.meeting_provider = "ZOOM"
    occurrence.provider_meeting_id = meeting_res["meeting_id"]

    await db.commit()
    await db.refresh(occurrence)

    target_url = occurrence.host_url if is_host else occurrence.meeting_url
    return {
        "join_url": occurrence.meeting_url,
        "host_url": occurrence.host_url,
        "target_url": target_url,
        "meeting_id": occurrence.provider_meeting_id,
        "provider": "ZOOM",
        "is_simulated": False
    }
