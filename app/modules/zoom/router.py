from app.common.webhook_body import read_webhook_body
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import EmailStr, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.core.config import settings
from app.core.database import get_db
from app.modules.auth.dependencies import get_current_user
from app.modules.organizations.dependencies import require_org_role
from app.common.enums import UserRole
from app.common.dates import ensure_utc
from app.models.organization import OrganizationMember
from app.models.schedule import ClassOccurrence, ClassOccurrenceChange
from app.models.zoom import TeacherZoomConnection, ZoomWebhookReceipt, ZoomOAuthAttempt
from . import service as svc
from .provider import ZoomProvider, ProviderError, mode
from .meetings import defaults, host_for_occurrence, persist_meeting, check_conflicts

router = APIRouter(tags=["Teacher Zoom"])
base = "/organizations/{org_id}/teachers/{member_id}/zoom"
owner_access = require_org_role([UserRole.STUDIO])


class CallbackRequest(BaseModel):
    state: str = Field(min_length=20, max_length=512)
    code: str = Field(min_length=1, max_length=4096)


class ConfirmRequest(BaseModel):
    confirmation_id: uuid.UUID


class RestartRequest(BaseModel):
    state: str = Field(min_length=20, max_length=512)
    kind: Literal["OAUTH", "REQUEST"] = "OAUTH"


@router.post("/integrations/zoom/restart")
async def restart_authorization(payload: RestartRequest, user=Depends(get_current_user), db=Depends(get_db)):
    return await svc.restart_authorization(db, payload.state, user, payload.kind)


@router.get(base)
async def get_state(org_id: uuid.UUID, member_id: uuid.UUID, user=Depends(get_current_user), db=Depends(get_db)):
    member = await svc.authorize_member(db, org_id, member_id, user)
    return await svc.teacher_state(db, member)


@router.post(base + "/authorization-request")
async def request_authorization(org_id: uuid.UUID, member_id: uuid.UUID, user=Depends(get_current_user), db=Depends(get_db)):
    member = await svc.authorize_member(db, org_id, member_id, user, owner_only=True)
    state, attempt = await svc.create_attempt(db, member, user, "REQUEST")
    return {"request_url": settings.WEBSITE_PUBLIC_ORIGIN + "/integrations/zoom/request?" + urlencode({"request": state}),
            "expires_at": attempt.expires_at.isoformat(), "intended_member_id": str(member.id)}


@router.post(base + "/connect")
async def connect(org_id: uuid.UUID, member_id: uuid.UUID, user=Depends(get_current_user), db=Depends(get_db), zoom_email: EmailStr | None = None, zoom_host_id: Annotated[str | None, Query(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")] = None):
    member = await svc.authorize_member(db, org_id, member_id, user, self_only=True)
    state, attempt = await svc.create_attempt(db, member, user, "OAUTH", expected_email=str(zoom_email) if zoom_email else None, expected_host_id=zoom_host_id)
    return {"authorization_url": ZoomProvider().authorize_url(state), "is_simulated": attempt.mode == "mock"}


class StudioConnectRequest(BaseModel):
    studio_password: str = Field(min_length=1, max_length=128)
    zoom_email: EmailStr | None = None
    zoom_host_id: str | None = Field(None, min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")

@router.post(base + "/studio-connect")
async def studio_connect(org_id: uuid.UUID, member_id: uuid.UUID, payload: StudioConnectRequest,
                         user=Depends(get_current_user), db=Depends(get_db)):
    from app.modules.teachers.credentials import reconfirm
    member = await svc.authorize_member(db, org_id, member_id, user, owner_only=True)
    await reconfirm(user, payload.studio_password)
    state, attempt = await svc.create_attempt(db, member, user, "OAUTH", expected_email=str(payload.zoom_email) if payload.zoom_email else None, expected_host_id=payload.zoom_host_id)
    return {"authorization_url": ZoomProvider().authorize_url(state), "is_simulated": attempt.mode == "mock"}


class RedeemRequest(BaseModel):
    request: str = Field(min_length=20, max_length=512)


@router.post("/integrations/zoom/request")
async def redeem(payload: RedeemRequest, user=Depends(get_current_user), db=Depends(get_db)):
    attempt = await svc.validate_attempt(db, payload.request, user, "REQUEST")
    attempt.consumed_at = svc.now()
    member = await db.get(OrganizationMember, attempt.member_id)
    await db.commit()
    state, fresh = await svc.create_attempt(db, member, user, "OAUTH")
    return {"authorization_url": ZoomProvider().authorize_url(state), "is_simulated": fresh.mode == "mock"}


@router.post("/integrations/zoom/callback")
async def oauth_callback(payload: CallbackRequest, user=Depends(get_current_user), db=Depends(get_db)):
    return await svc.callback(db, payload.state, payload.code, user)


@router.post("/integrations/zoom/confirm")
async def confirm(payload: ConfirmRequest, user=Depends(get_current_user), db=Depends(get_db)):
    return await svc.confirm(db, payload.confirmation_id, user)


@router.get(base + "/impact")
async def connection_impact(org_id: uuid.UUID, member_id: uuid.UUID, user=Depends(get_current_user), db=Depends(get_db)):
    await svc.authorize_member(db, org_id, member_id, user)
    return await svc.impact(db, org_id, member_id)


@router.delete(base)
async def disconnect(org_id: uuid.UUID, member_id: uuid.UUID, acknowledge_impact: bool = False, user=Depends(get_current_user), db=Depends(get_db)):
    member = await svc.authorize_member(db, org_id, member_id, user)
    result = await svc.disconnect(db, member, acknowledge_impact)
    result["revocations"] = await svc.process_revocations(db, org_id)
    return result


class DefaultsRequest(BaseModel):
    version: int
    default_host_member_id: uuid.UUID | None = None
    fallback_enabled: bool = True
    auto_meeting_enabled: bool = False
    waiting_room: bool = True
    host_video: bool = True
    join_before_host: bool = False
    enforce_zoom_login: bool = False
    enforce_basic_login: bool = True
    group_allow_self_buy_subscriptions: bool = True
    group_active_subscription_required: bool = True
    group_confirm_checkin_everytime: bool = True
    personal_allow_self_buy_subscriptions: bool = True
    personal_active_subscription_required: bool = True
    personal_confirm_checkin_everytime: bool = True
    personal_no_show_penalty: bool = True
    acknowledge_impact: bool = False


async def get_defaults(db, org_id):
    prefs = await svc.settings_for(db, org_id)
    conn = await svc.connection_for(db, prefs.default_zoom_host_member_id) if prefs.default_zoom_host_member_id else None
    usable = False
    try:
        usable = await svc.eligible(db, conn)
        provider_mode = mode()
    except HTTPException:
        provider_mode = "disabled"
    result = {k: getattr(prefs, k) for k in (
        "zoom_auto_meeting_enabled", "zoom_waiting_room", "zoom_host_video", "zoom_join_before_host", "zoom_enforce_zoom_login",
        "zoom_enforce_basic_login", "zoom_group_allow_self_buy_subscriptions", "zoom_group_active_subscription_required", "zoom_group_confirm_checkin_everytime",
        "zoom_personal_allow_self_buy_subscriptions", "zoom_personal_active_subscription_required", "zoom_personal_confirm_checkin_everytime", "zoom_personal_no_show_penalty")}
    members = (await db.scalars(select(OrganizationMember).where(OrganizationMember.organization_id == org_id, OrganizationMember.can_teach.is_(True)))).all()
    result.update(version=prefs.zoom_settings_version, default_host_member_id=str(prefs.default_zoom_host_member_id) if prefs.default_zoom_host_member_id else None,
        fallback_enabled=prefs.zoom_fallback_enabled, default_host=svc.summary(conn) if usable else None,
        zoom_connected=usable, zoom_account_email=conn.email if usable else None,
        provider_mode=provider_mode, production_ready=usable and conn.mode == "live")
    # Explicit user fetch avoids async lazy-loading and includes inactive connections for recovery.
    from app.models.user import User
    result["teachers"] = [dict(id=str(m.id), user_id=str(m.user_id), name=(await db.get(User, m.user_id)).full_name,
                              is_active=m.is_active, is_owner=m.role == "STUDIO", **(await svc.teacher_state(db, m))) for m in members]
    return result


async def save_defaults(db, org_id, payload):
    await svc.lock_studio(db, org_id)
    prefs = await svc.settings_for(db, org_id)
    if prefs.zoom_settings_version != payload.version:
        raise HTTPException(409, "Zoom settings changed; reload before saving")
    if payload.default_host_member_id:
        conn = await svc.connection_for(db, payload.default_host_member_id)
        if not conn or conn.organization_id != org_id or not await svc.eligible(db, conn):
            raise HTTPException(422, "Default host must have an active connection in this studio")
    if payload.auto_meeting_enabled and not payload.default_host_member_id:
        raise HTTPException(422, "Choose an active default host before enabling automatic Zoom classes")
    if prefs.default_zoom_host_member_id != payload.default_host_member_id and prefs.default_zoom_host_member_id and not payload.acknowledge_impact:
        raise HTTPException(409, "Confirm the default-host change; existing meetings retain their saved host")
    prefs.default_zoom_host_member_id = payload.default_host_member_id
    prefs.zoom_fallback_enabled = payload.fallback_enabled
    for name, value in payload.model_dump().items():
        if name not in {"version", "default_host_member_id", "fallback_enabled", "acknowledge_impact"}:
            setattr(prefs, "zoom_" + name, value)
    prefs.zoom_settings_version += 1
    await db.commit()
    return dict(status="success", **await get_defaults(db, org_id))


@router.post("/organizations/{org_id}/zoom/retry-revocations")
async def retry_revocations(org_id: uuid.UUID, tenant=Depends(owner_access), db=Depends(get_db)):
    return await svc.process_revocations(db, org_id)


class ApplyRequest(BaseModel):
    version: int
    apply: bool = False


@router.post("/organizations/{org_id}/zoom/apply-future-settings")
async def apply_future(org_id: uuid.UUID, payload: ApplyRequest, tenant=Depends(owner_access), db=Depends(get_db)):
    prefs = await svc.settings_for(db, org_id)
    if payload.version != prefs.zoom_settings_version:
        raise HTTPException(409, "Reload settings before previewing this operation")
    rows = (await db.scalars(select(ClassOccurrence).where(ClassOccurrence.organization_id == org_id,
        ClassOccurrence.actual_start_at > svc.now(), ClassOccurrence.status.in_(["SCHEDULED", "RESCHEDULED"]), ClassOccurrence.zoom_connection_id.is_not(None)))).all()
    occurrence_ids = [o.id for o in rows]
    effective_defaults = defaults(prefs)
    results = []
    for occurrence_id in occurrence_ids:
        occ = await db.get(ClassOccurrence, occurrence_id)
        saved_snapshot = occ.zoom_settings_snapshot or {}
        if saved_snapshot.get("version") == payload.version and not saved_snapshot.get("restricted_settings"):
            results.append({"id": str(occ.id), "status": "CURRENT"})
            continue
        if not payload.apply:
            results.append({"id": str(occ.id), "host_email": occ.zoom_host_email, "status": "PREVIEW"})
            continue
        try:
            conn = await host_for_occurrence(db, occ)
            token = await svc.access_token(db, conn)
            effective = dict(effective_defaults)
            await ZoomProvider(conn.mode).update(token, occ.provider_meeting_id, {"settings": {k: v for k, v in effective.items() if k != "version"}})
            if conn.mode == "live":
                data = await ZoomProvider(conn.mode).meeting(token, occ.provider_meeting_id)
                actual = data.get("settings", {})
                effective["restricted_settings"] = [k for k, v in effective.items() if k not in {"version", "restricted_settings"} and k in actual and actual[k] != v]
                effective["provider_effective"] = actual
            occ.zoom_settings_snapshot = effective
            await db.commit()
            results.append({"id": str(occ.id), "status": "UPDATED", "restricted_settings": effective.get("restricted_settings", [])})
        except (HTTPException, ProviderError) as exc:
            await db.rollback()
            results.append({"id": str(occurrence_id), "status": "FAILED", "message": exc.detail if isinstance(exc, HTTPException) else str(exc)})
    return {"count": len(occurrence_ids), "results": results}


class ReconcileRequest(BaseModel):
    replacement_member_id: uuid.UUID | None = None
    confirm_links_change: bool = False


@router.post("/organizations/{org_id}/occurrences/{occurrence_id}/zoom/reconcile")
async def reconcile(org_id: uuid.UUID, occurrence_id: uuid.UUID, payload: ReconcileRequest, user=Depends(get_current_user), tenant=Depends(owner_access), db=Depends(get_db)):
    await svc.lock_studio(db, org_id)
    occ = await db.scalar(select(ClassOccurrence).where(ClassOccurrence.id == occurrence_id, ClassOccurrence.organization_id == org_id).with_for_update())
    if not occ:
        raise HTTPException(404, "Class not found")
    if occ.actual_start_at and ensure_utc(occ.actual_start_at) <= svc.now() or occ.status in {"CANCELLED", "COMPLETED"}:
        raise HTTPException(409, "Only future active classes can change Zoom hosts")
    if occ.zoom_provisioning_status in {"CREATING", "UNCERTAIN"}:
        conn = await host_for_occurrence(db, occ)
        token = await svc.access_token(db, conn)
        try:
            data = await ZoomProvider(conn.mode).reconcile(token, occ.zoom_operation_key)
        except ProviderError as exc:
            raise HTTPException(503, str(exc))
        if not data:
            raise HTTPException(409, "No matching provider meeting found yet; inspect Zoom and retry reconciliation. No new meeting was created.")
        persist_meeting(occ, conn, data)
        await db.commit()
        return {"status": "RECONCILED", "join_url": occ.meeting_url}
    if not payload.replacement_member_id or not payload.confirm_links_change:
        return {"status": "PREVIEW", "old_host": occ.zoom_host_email, "old_join_url": occ.meeting_url,
                "warning": "Replacement creates a new link. The previous account holder must cancel the old Zoom meeting separately; history is retained."}
    conn = await svc.resolve_host(db, org_id, explicit_member_id=payload.replacement_member_id)
    await check_conflicts(db, occ, conn)
    db.add(ClassOccurrenceChange(class_occurrence_id=occ.id, changed_by_user_id=user.id, change_type="ZOOM_HOST_REPLACEMENT",
        reason=json.dumps({"previous_host": occ.zoom_host_email, "previous_meeting_id": occ.provider_meeting_id, "previous_connection_id": str(occ.zoom_connection_id), "previous_connection_version": occ.zoom_connection_version,
                           "previous_join_url": occ.meeting_url, "previous_settings": occ.zoom_settings_snapshot})))
    occ.zoom_connection_id, occ.zoom_connection_version = conn.id, conn.version
    occ.zoom_host_member_id, occ.zoom_host_email = conn.member_id, conn.email
    occ.provider_meeting_id, occ.meeting_url, occ.host_url = None, None, None
    occ.zoom_provisioning_status = "NOT_PROVISIONED"
    await db.commit()
    return {"status": "READY_TO_PROVISION", "links_will_change": True}


@router.post("/integrations/zoom/deauthorization")
async def deauthorization(request: Request, db=Depends(get_db)):
    secret = settings.ZOOM_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(503, "Zoom webhook is not configured")
    raw = await read_webhook_body(request)
    stamp = request.headers.get("x-zm-request-timestamp", "")
    try:
        if abs(svc.now().timestamp() - int(stamp)) > 300:
            raise ValueError()
    except ValueError:
        raise HTTPException(401, "Expired Zoom webhook")
    expected = "v0=" + hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), request.headers.get("x-zm-signature", "").encode()):
        raise HTTPException(401, "Invalid Zoom webhook signature")
    try:
        data = json.loads(raw)
        payload = data["payload"]
        if not isinstance(data, dict) or not isinstance(payload, dict):
            raise ValueError()
    except (ValueError, KeyError, TypeError, RecursionError):
        raise HTTPException(400, "Invalid Zoom webhook")
    if data.get("event") == "endpoint.url_validation":
        plain = payload.get("plainToken", "")
        if not isinstance(plain, str):
            raise HTTPException(400, "Invalid Zoom webhook")
        return {"plainToken": plain, "encryptedToken": hmac.new(secret.encode(), plain.encode(), hashlib.sha256).hexdigest()}
    if data.get("event") not in {"app_deauthorized", "app.deauthorized"}:
        return {"status": "ignored"}
    if payload.get("client_id") != settings.ZOOM_CLIENT_ID or not payload.get("user_id") or not payload.get("account_id"):
        raise HTTPException(400, "Deauthorization must identify this app, account and user")
    key = hashlib.sha256(raw).hexdigest()
    if await db.get(ZoomWebhookReceipt, key):
        return {"status": "already_processed"}
    db.add(ZoomWebhookReceipt(digest=key, received_at=svc.now()))
    rows = (await db.scalars(select(TeacherZoomConnection).where(TeacherZoomConnection.zoom_user_id == payload["user_id"], TeacherZoomConnection.zoom_account_id == payload["account_id"]))).all()
    try:
        if data.get("event_ts"):
            event_time = float(data["event_ts"])
            if event_time > 10**11:
                event_time /= 1000
        else:
            event_time = ensure_utc(datetime.fromisoformat(payload["deauthorization_time"].replace("Z", "+00:00"))).timestamp()
    except (TypeError, ValueError, KeyError):
        raise HTTPException(400, "Deauthorization time is missing or invalid")
    for conn in rows:
        await svc.lock_studio(db, conn.organization_id)
        conn = await db.scalar(select(TeacherZoomConnection).where(TeacherZoomConnection.id == conn.id).with_for_update().execution_options(populate_existing=True))
        # Delayed old events cannot invalidate a newer authorization.
        if conn.authorized_at and event_time < ensure_utc(conn.authorized_at).timestamp():
            continue
        if conn.status == "RECONNECT_REQUIRED" and conn.tokens_enc is None:
            continue
        conn.status, conn.tokens_enc, conn.expires_at = "RECONNECT_REQUIRED", None, None
        conn.version += 1
        prefs = await svc.settings_for(db, conn.organization_id)
        if prefs.default_zoom_host_member_id == conn.member_id:
            prefs.zoom_auto_meeting_enabled = False
            prefs.zoom_settings_version += 1
        for attempt in (await db.scalars(select(ZoomOAuthAttempt).where(ZoomOAuthAttempt.member_id == conn.member_id))).all():
            attempt.candidate_enc, attempt.consumed_at = None, svc.now()
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
    return {"status": "processed"}


@router.get("/organizations/{org_id}/zoom/meetings")
async def saved_meetings(org_id: uuid.UUID, tenant=Depends(owner_access), db=Depends(get_db)):
    rows = (await db.scalars(select(ClassOccurrence).where(ClassOccurrence.organization_id == org_id,
        ClassOccurrence.actual_start_at > svc.now(), ClassOccurrence.status.in_(["SCHEDULED", "RESCHEDULED"]),
        (ClassOccurrence.zoom_connection_id.is_not(None)) | (ClassOccurrence.meeting_provider == "ZOOM")).order_by(ClassOccurrence.actual_start_at))).all()
    result = []
    for occ in rows:
        from app.models.session import Session
        session = await db.get(Session, occ.session_id)
        state = occ.zoom_provisioning_status
        try:
            await host_for_occurrence(db, occ)
        except HTTPException:
            state = "ACTION_REQUIRED"
        result.append({"id": str(occ.id), "class_name": session.name, "start_at": occ.actual_start_at.isoformat(),
                       "host_email": occ.zoom_host_email, "status": state})
    return result
