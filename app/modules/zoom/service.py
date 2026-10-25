import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from app.common.dates import ensure_utc
from app.core.config import settings
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.models.schedule import ClassOccurrence
from app.models.zoom import TeacherZoomConnection, ZoomOAuthAttempt, ZoomRevocation
from .provider import ZoomProvider, ProviderError, seal, unseal, mode

USABLE = {"ACTIVE", "SIMULATED"}


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


async def lock_studio(db, org_id):
    await db.scalar(select(Organization.id).where(Organization.id == org_id).with_for_update())


async def settings_for(db, org_id):
    obj = await db.scalar(select(OrganizationSetting).where(OrganizationSetting.organization_id == org_id))
    if not obj:
        obj = OrganizationSetting(organization_id=org_id, zoom_auto_meeting_enabled=False)
        db.add(obj)
        await db.flush()
    return obj


async def connection_for(db, member_id, lock=False):
    stmt = select(TeacherZoomConnection).where(TeacherZoomConnection.member_id == member_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    return await db.scalar(stmt)


async def eligible(db, conn):
    if not conn or conn.status not in USABLE or conn.mode != mode():
        return False
    member = await db.get(OrganizationMember, conn.member_id)
    return bool(member and member.is_active and member.can_teach and member.organization_id == conn.organization_id)


def summary(conn):
    if not conn:
        return None
    return {"connection_id": str(conn.id), "member_id": str(conn.member_id), "zoom_host_id": conn.zoom_user_id, "email": conn.email,
            "display_name": conn.display_name, "status": conn.status, "is_simulated": conn.mode == "mock", "version": conn.version,
            "production_ready": conn.status == "ACTIVE" and conn.mode == "live"}


async def resolve_host(db, org_id, member_id=None, explicit_member_id=None):
    prefs = await settings_for(db, org_id)
    candidate = explicit_member_id or member_id
    if candidate:
        member = await db.get(OrganizationMember, candidate)
        if not member or member.organization_id != org_id or not member.is_active or not member.can_teach:
            raise HTTPException(422, "Host teacher must be active and belong to this studio")
        personal = await connection_for(db, candidate)
        if await eligible(db, personal):
            return personal
        if explicit_member_id:
            raise HTTPException(409, "The selected host needs Zoom authorization")
        if personal and personal.status == "RECONNECT_REQUIRED":
            raise HTTPException(409, "Assigned teacher must reconnect Zoom; choose a replacement explicitly")
    if prefs.zoom_fallback_enabled and prefs.default_zoom_host_member_id:
        conn = await connection_for(db, prefs.default_zoom_host_member_id)
        if await eligible(db, conn) and conn.organization_id == org_id:
            return conn
    raise HTTPException(409, "Set up a personal Zoom connection or an active studio default host")


async def teacher_state(db, member):
    prefs = await settings_for(db, member.organization_id)
    conn = await connection_for(db, member.id)
    host = None
    try:
        host = await resolve_host(db, member.organization_id, member.id)
    except HTTPException:
        pass
    attempt = await db.scalar(select(ZoomOAuthAttempt.id).where(ZoomOAuthAttempt.member_id == member.id,
        ZoomOAuthAttempt.consumed_at.is_(None), ZoomOAuthAttempt.expires_at > now()).limit(1))
    status = conn.status if conn else "NOT_CONNECTED"
    if attempt and status not in USABLE:
        status = "PENDING"
    host_member = await db.get(OrganizationMember, host.member_id) if host else None
    return {"zoom_connection_status": status,
            "fallback_host_is_owner": bool(host_member and host_member.role == "STUDIO" and host.member_id != member.id), "zoom_connection": summary(conn),
            "zoom_verified_email": conn.email if conn and conn.status in USABLE else None,
            "is_default_zoom_host": prefs.default_zoom_host_member_id == member.id,
            "effective_zoom_host": summary(host), "zoom_ready": host is not None,
            "production_ready": bool(host and host.mode == "live" and host.status == "ACTIVE"),
            "start_responsibility": "The connected Zoom account holder must start the meeting" if host else "Zoom setup required"}


async def authorize_member(db, org_id, member_id, user, owner_only=False, self_only=False):
    org = await db.get(Organization, org_id)
    actor = await db.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == org_id,
        OrganizationMember.user_id == user.id, OrganizationMember.is_active.is_(True)))
    target = await db.get(OrganizationMember, member_id)
    if not org or org.deleted_at or org.status != "ACTIVE" or not actor or actor.role != user.role or actor.role not in {"STUDIO", "TEACHER"}:
        raise HTTPException(403, "Studio staff access required")
    if not target or target.organization_id != org_id or not target.can_teach or not target.is_active:
        raise HTTPException(404, "Active teaching member not found in this studio")
    if owner_only and actor.role != "STUDIO":
        raise HTTPException(403, "Studio owner access required")
    if self_only and target.user_id != user.id:
        raise HTTPException(403, "Only this teacher can consent to connect their Zoom account; send a request link")
    if actor.role != "STUDIO" and target.user_id != user.id:
        raise HTTPException(403, "You may manage only your own Zoom connection")
    from app.modules.billing.saas import require_operational_access
    await require_operational_access(db, org_id)
    return target


async def create_attempt(db, member, user, kind, expected_email=None, expected_host_id=None):
    if kind == "REQUEST":
        selected_mode = settings.ZOOM_PROVIDER_MODE if settings.ZOOM_PROVIDER_MODE in {"mock", "live"} else "live"
    else:
        selected_mode = mode()
    await lock_studio(db, member.organization_id)
    conn = await connection_for(db, member.id)
    state = secrets.token_urlsafe(40)
    attempt = ZoomOAuthAttempt(state_hash=digest(state), organization_id=member.organization_id, member_id=member.id,
        actor_id=user.id, intended_user_id=user.id if kind == "OAUTH" else member.user_id, expected_email=expected_email, expected_host_id=expected_host_id, kind=kind, mode=selected_mode,
        connection_version=conn.version if conn else 0, expires_at=now() + timedelta(minutes=15))
    db.add(attempt)
    await db.commit()
    return state, attempt


async def validate_attempt(db, state, user, kind):
    row = await db.scalar(select(ZoomOAuthAttempt).where(ZoomOAuthAttempt.state_hash == digest(state)).with_for_update())
    if not row or row.kind != kind or row.consumed_at or ensure_utc(row.expires_at) <= now():
        raise HTTPException(400, "Authorization link expired or already used; request a new link")
    if row.intended_user_id != user.id:
        raise HTTPException(403, "Sign in as the teacher named in this authorization request")
    await authorize_member(db, row.organization_id, row.member_id, user, self_only=row.kind == "REQUEST")
    initiator = await db.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == row.organization_id,
        OrganizationMember.user_id == row.actor_id, OrganizationMember.is_active.is_(True)))
    if not initiator or (row.kind == "REQUEST" and initiator.role != "STUDIO"):
        raise HTTPException(403, "The request initiator no longer has studio access")
    if row.mode != mode():
        raise HTTPException(409, "Zoom environment changed; start authorization again")
    conn = await connection_for(db, row.member_id)
    if (conn.version if conn else 0) != row.connection_version:
        raise HTTPException(409, "Zoom connection changed; start authorization again")
    return row


async def callback(db, state, code, user):
    attempt = await validate_attempt(db, state, user, "OAUTH")
    attempt.consumed_at = now()
    attempt_id, member_id, selected_mode = attempt.id, attempt.member_id, attempt.mode
    await db.commit()  # Consume before network I/O; a failed exchange needs a fresh state.
    member = await db.get(OrganizationMember, member_id)
    try:
        tokens, identity = await ZoomProvider(selected_mode).exchange(code, state, member)
    except ProviderError as exc:
        raise HTTPException(502, str(exc))
    if attempt.expected_host_id and identity["id"] != attempt.expected_host_id:
        raise HTTPException(422, "Authorized Zoom Host ID does not match the requested host; start again with the correct Zoom account")
    if attempt.expected_email and identity["email"].lower() != attempt.expected_email.lower():
        raise HTTPException(422, "Authorized Zoom email does not match the requested email; start again with the correct account")
    # Persist a short-lived candidate. Explicit confirmation prevents accidental replacement.
    attempt = await db.get(ZoomOAuthAttempt, attempt_id)
    attempt.candidate_enc = seal({"tokens": tokens, "identity": identity})
    attempt.expires_at = now() + timedelta(minutes=5)
    await db.commit()
    return {"confirmation_id": str(attempt.id), "email": identity["email"],
            "display_name": identity.get("display_name") or identity.get("first_name", ""), "is_simulated": selected_mode == "mock"}


async def queue_revoke(db, conn):
    conn.host_key_enc = None
    if conn.tokens_enc:
        db.add(ZoomRevocation(organization_id=conn.organization_id, mode=conn.mode, zoom_user_id=conn.zoom_user_id, token_enc=conn.tokens_enc,
                              created_at=now(), status="PENDING"))
    conn.tokens_enc = None
    conn.expires_at = None


async def confirm(db, confirmation_id, user):
    attempt = await db.get(ZoomOAuthAttempt, confirmation_id)
    if not attempt:
        raise HTTPException(404, "Authorization confirmation not found")
    await lock_studio(db, attempt.organization_id)
    attempt = await db.scalar(select(ZoomOAuthAttempt).where(ZoomOAuthAttempt.id == confirmation_id).with_for_update().execution_options(populate_existing=True))
    if not attempt.candidate_enc or ensure_utc(attempt.expires_at) <= now() or attempt.intended_user_id != user.id:
        raise HTTPException(400, "Authorization confirmation expired or already used")
    member = await authorize_member(db, attempt.organization_id, attempt.member_id, user)
    conn = await connection_for(db, member.id, lock=True)
    if (conn.version if conn else 0) != attempt.connection_version or mode() != attempt.mode:
        raise HTTPException(409, "Connection changed; authorize again")
    data = unseal(attempt.candidate_enc)
    identity, tokens = data["identity"], data["tokens"]
    # V1: one provider grant can belong to only one member in the entire platform.
    other = await db.scalar(select(TeacherZoomConnection.id).where(TeacherZoomConnection.zoom_user_id == identity["id"],
        TeacherZoomConnection.status.in_(USABLE), TeacherZoomConnection.member_id != member.id))
    pending_revocation = await db.scalar(select(ZoomRevocation.id).where(ZoomRevocation.zoom_user_id == identity["id"], ZoomRevocation.status == "PENDING").limit(1))
    if pending_revocation:
        raise HTTPException(409, "An earlier provider revocation is pending. Retry revocations, then begin a fresh authorization.")
    if other:
        raise HTTPException(409, "This Zoom identity is already connected to another studio member; use studio host fallback")
    if not conn:
        conn = TeacherZoomConnection(organization_id=member.organization_id, member_id=member.id, mode=attempt.mode, version=0, token_version=0)
        db.add(conn)
    if conn.tokens_enc and conn.zoom_user_id != identity["id"]:
        await queue_revoke(db, conn)
    conn.zoom_user_id, conn.zoom_account_id = identity["id"], identity["account_id"]
    conn.email, conn.display_name = identity["email"], identity.get("display_name") or identity.get("first_name", "")
    conn.mode, conn.status = attempt.mode, "SIMULATED" if attempt.mode == "mock" else "ACTIVE"
    conn.tokens_enc, conn.expires_at, conn.scopes = seal(tokens), now() + timedelta(seconds=int(tokens["expires_in"])), tokens.get("scope", "")
    conn.version += 1
    conn.token_version += 1
    conn.authorized_at = now()
    attempt.candidate_enc = None
    prefs = await settings_for(db, member.organization_id)
    if member.role == "STUDIO" and not prefs.default_zoom_host_member_id:
        prefs.default_zoom_host_member_id = member.id
        prefs.zoom_settings_version += 1
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This Zoom identity is already in use; refresh connection status")
    return await teacher_state(db, member)


async def access_token(db, conn):
    conn = await db.scalar(select(TeacherZoomConnection).where(TeacherZoomConnection.id == conn.id).with_for_update().execution_options(populate_existing=True))
    if not await eligible(db, conn) or not conn.tokens_enc:
        raise HTTPException(409, "Zoom host must reconnect before this operation")
    tokens = unseal(conn.tokens_enc)
    if not conn.expires_at or ensure_utc(conn.expires_at) < now() + timedelta(seconds=90):
        try:
            tokens = await ZoomProvider(conn.mode).refresh(tokens)
        except ProviderError as exc:
            if exc.invalid_grant:
                conn.status = "RECONNECT_REQUIRED"
                conn.tokens_enc = None
                await db.commit()
            raise HTTPException(409 if exc.invalid_grant else 503, str(exc))
        conn.tokens_enc = seal(tokens)
        conn.expires_at = now() + timedelta(seconds=int(tokens["expires_in"]))
        conn.token_version += 1
        await db.flush()
    # Caller retains the row lock through the provider operation to serialize refresh/disconnect.
    return tokens["access_token"]


async def impact(db, org_id, member_id):
    prefs = await settings_for(db, org_id)
    rows = (await db.scalars(select(ClassOccurrence).where(ClassOccurrence.organization_id == org_id,
        ClassOccurrence.actual_start_at >= now(), ClassOccurrence.status.in_(["SCHEDULED", "RESCHEDULED", "IN_PROGRESS"]),
        (ClassOccurrence.zoom_host_member_id == member_id) | ((ClassOccurrence.zoom_connection_id.is_(None)) & (ClassOccurrence.instructor_id == member_id))))).all()
    if prefs.default_zoom_host_member_id == member_id:
        fallback = (await db.scalars(select(ClassOccurrence).where(ClassOccurrence.organization_id == org_id,
            ClassOccurrence.actual_start_at >= now(), ClassOccurrence.zoom_connection_id.is_(None),
            ClassOccurrence.status.in_(["SCHEDULED", "RESCHEDULED"])))) .all()
        rows = list({o.id: o for o in rows + list(fallback)}.values())
    return {"future_class_count": len(rows), "occurrence_ids": [str(o.id) for o in rows],
            "is_default_host": prefs.default_zoom_host_member_id == member_id,
            "message": "Existing Zoom meetings remain; their account holder must manage them. New provisioning will pause if the default host is removed."}


async def disconnect(db, member, pause_confirmed, commit=True):
    await lock_studio(db, member.organization_id)
    preview = await impact(db, member.organization_id, member.id)
    if (preview["future_class_count"] or preview["is_default_host"]) and not pause_confirmed:
        raise HTTPException(409, "Review class impact and confirm pausing new provisioning")
    conn = await connection_for(db, member.id, lock=True)
    if conn and conn.status != "DISCONNECTED":
        await queue_revoke(db, conn)
        conn.status = "DISCONNECTED"
        conn.version += 1
    prefs = await settings_for(db, member.organization_id)
    if prefs.default_zoom_host_member_id == member.id:
        prefs.default_zoom_host_member_id = None
        prefs.zoom_auto_meeting_enabled = False
        prefs.zoom_settings_version += 1
    # Outstanding candidates must never reactivate a disconnected grant.
    attempts = (await db.scalars(select(ZoomOAuthAttempt).where(ZoomOAuthAttempt.member_id == member.id))).all()
    for attempt in attempts:
        if attempt.candidate_enc:
            data = unseal(attempt.candidate_enc)
            other = await db.scalar(select(TeacherZoomConnection.id).where(TeacherZoomConnection.zoom_user_id == data["identity"]["id"],
                TeacherZoomConnection.member_id != member.id, TeacherZoomConnection.status.in_(USABLE)).limit(1))
            if not other:
                db.add(ZoomRevocation(organization_id=member.organization_id, mode=attempt.mode, zoom_user_id=data["identity"]["id"],
                    token_enc=seal(data["tokens"]), created_at=now(), status="PENDING"))
        attempt.candidate_enc, attempt.consumed_at = None, now()
    if commit:
        await db.commit()
    return {"status": "DISCONNECTED", "impact": preview, "provider_revocation": "queued"}


async def process_revocations(db, org_id=None):
    stmt = select(ZoomRevocation).where(ZoomRevocation.status == "PENDING").with_for_update(skip_locked=True)
    if org_id:
        stmt = stmt.where(ZoomRevocation.organization_id == org_id)
    rows = (await db.scalars(stmt.limit(100))).all()
    for row in rows:
        try:
            if row.mode == "mock":
                row.status, row.token_enc = "REVOKED", None
            elif now() - ensure_utc(row.created_at) > timedelta(hours=24):
                row.status, row.token_enc = "EXPIRED", None
            else:
                await ZoomProvider(row.mode).revoke(unseal(row.token_enc))
                row.status, row.token_enc = "REVOKED", None
        except (ProviderError, HTTPException):
            pass
    await db.commit()
    # Purge unconfirmed authorization tokens after their five-minute confirmation window.
    expired_query = select(ZoomOAuthAttempt).where(ZoomOAuthAttempt.expires_at < now(), ZoomOAuthAttempt.candidate_enc.is_not(None))
    if org_id:
        expired_query = expired_query.where(ZoomOAuthAttempt.organization_id == org_id)
    expired = (await db.scalars(expired_query)).all()
    for row in expired:
        data = unseal(row.candidate_enc)
        active_binding = await db.scalar(select(TeacherZoomConnection.id).where(TeacherZoomConnection.zoom_user_id == data["identity"]["id"], TeacherZoomConnection.status.in_(USABLE)).limit(1))
        if not active_binding:
            db.add(ZoomRevocation(organization_id=row.organization_id, mode=row.mode, zoom_user_id=data["identity"]["id"], token_enc=seal(data["tokens"]), created_at=now(), status="PENDING"))
        row.candidate_enc = None
    await db.commit()
    pending_query = select(func.count(ZoomRevocation.id)).where(ZoomRevocation.status == "PENDING")
    expired_query = select(func.count(ZoomRevocation.id)).where(ZoomRevocation.status == "EXPIRED")
    if org_id:
        pending_query = pending_query.where(ZoomRevocation.organization_id == org_id)
        expired_query = expired_query.where(ZoomRevocation.organization_id == org_id)
    return {"pending": await db.scalar(pending_query), "manual_marketplace_removal_required": await db.scalar(expired_query)}


async def default_host_ready(db, org_id):
    prefs = await settings_for(db, org_id)
    if not prefs.default_zoom_host_member_id:
        return False
    try:
        return await eligible(db, await connection_for(db, prefs.default_zoom_host_member_id))
    except HTTPException:
        return False
