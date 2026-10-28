"""Persisted host routing, durable create intent and conservative timeout recovery."""
import uuid
from fastapi import HTTPException
from sqlalchemy import select
from app.models.schedule import ClassOccurrence
from app.models.session import Session, SessionInstructor
from app.models.organization import OrganizationMember
from app.models.zoom import TeacherZoomConnection
from app.common.dates import ensure_utc
from .provider import ZoomProvider, ProviderError
from .service import settings_for, resolve_host, lock_studio, access_token, eligible


def defaults(prefs):
    return {"waiting_room": prefs.zoom_waiting_room, "host_video": prefs.zoom_host_video,
            "join_before_host": prefs.zoom_join_before_host, "meeting_authentication": prefs.zoom_enforce_zoom_login,
            "auto_recording": "none", "version": prefs.zoom_settings_version}


async def host_for_occurrence(db, occ):
    if occ.zoom_connection_id:
        conn = await db.get(TeacherZoomConnection, occ.zoom_connection_id)
        if not conn or conn.organization_id != occ.organization_id or conn.version != occ.zoom_connection_version or not await eligible(db, conn):
            raise HTTPException(409, "The saved Zoom host changed or disconnected; reconcile this class explicitly")
        return conn
    member_id = occ.instructor_id
    instructors = (await db.scalars(select(SessionInstructor).where(SessionInstructor.session_id == occ.session_id))).all()
    primary = [i for i in instructors if i.role == "PRIMARY"]
    if len(instructors) > 1 and len(primary) != 1:
        raise HTTPException(409, "Select exactly one primary teacher for this class")
    if not member_id:
        member_id = primary[0].member_id if primary else instructors[0].member_id if instructors else None
    return await resolve_host(db, occ.organization_id, member_id)


async def check_conflicts(db, occ, conn, start=None, end=None):
    start, end = start or occ.actual_start_at, end or occ.actual_end_at
    rows = (await db.scalars(select(ClassOccurrence).where(ClassOccurrence.organization_id == occ.organization_id,
        ClassOccurrence.id != occ.id, ClassOccurrence.status.in_(["SCHEDULED", "RESCHEDULED", "IN_PROGRESS"]),
        ClassOccurrence.actual_start_at < end, ClassOccurrence.actual_end_at > start))).all()
    for other in rows:
        try:
            host = await host_for_occurrence(db, other)
        except HTTPException:
            continue
        if host.id == conn.id:
            raise HTTPException(409, "The resolved Zoom host already has an overlapping class; choose another authorized host or time")


async def meeting_access(db, occurrence, org, *, is_host=False, actor_user_id=None):
    from app.modules.billing.saas import require_operational_access
    await require_operational_access(db, org.id)
    await lock_studio(db, org.id)
    occ = await db.scalar(select(ClassOccurrence).where(ClassOccurrence.id == occurrence.id, ClassOccurrence.organization_id == org.id)
        .with_for_update().execution_options(populate_existing=True))
    if not occ or occ.status == "CANCELLED":
        raise HTTPException(409, "This class is no longer joinable")
    session = await db.get(Session, occ.session_id)
    if not session or ((session.deleted_at or (not session.is_active and session.session_type != "ONE_TO_ONE")) and occ.status != "COMPLETED"):
        raise HTTPException(409, "This class is inactive")
    # Explicit, externally managed class links remain participant links, never Zoom host credentials.
    if occ.meeting_url and not occ.meeting_provider and not occ.zoom_connection_id:
        return {"join_url": occ.meeting_url, "target_url": occ.meeting_url, "provider": "EXTERNAL", "is_simulated": False,
                "host_url": None, "host_email": None, "can_start": False}
    if occ.meeting_provider == "ZOOM" and not occ.zoom_connection_id:
        raise HTTPException(409, "Legacy Zoom meeting requires explicit owner reconciliation after OAuth setup")
    conn = await host_for_occurrence(db, occ)
    if not occ.provider_meeting_id:
        prefs = await settings_for(db, org.id)
        if not prefs.zoom_auto_meeting_enabled:
            raise HTTPException(409, "Automatic Zoom provisioning is paused in Zoom Settings")
        if occ.zoom_provisioning_status in {"CREATING", "UNCERTAIN"}:
            raise HTTPException(409, "Meeting creation needs reconciliation; retrying cannot create another meeting")
        await check_conflicts(db, occ, conn)
        snapshot = defaults(prefs)
        occ.zoom_connection_id, occ.zoom_connection_version = conn.id, conn.version
        occ.zoom_host_member_id, occ.zoom_host_email = conn.member_id, conn.email
        occ.zoom_settings_snapshot = snapshot
        occ.zoom_operation_key = str(uuid.uuid4())
        occ.zoom_provisioning_status = "CREATING"
        operation = occ.zoom_operation_key
        await db.commit()  # Durable intent survives a provider success followed by a lost response/process crash.
        await lock_studio(db, org.id)
        # Recheck after the commit: cancellation/disconnect cannot race a queued operation.
        occ = await db.scalar(select(ClassOccurrence).where(ClassOccurrence.id == occurrence.id).with_for_update().execution_options(populate_existing=True))
        if occ.status == "CANCELLED":
            raise HTTPException(409, "This class is no longer joinable")
        conn = await host_for_occurrence(db, occ)
        token = await access_token(db, conn)
        session = await db.get(Session, occ.session_id)
        payload = {"topic": session.name, "type": 2,
                   "start_time": ensure_utc(occ.actual_start_at).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "duration": max(1, int((occ.actual_end_at - occ.actual_start_at).total_seconds() / 60)),
                   "timezone": org.timezone, "settings": {k: v for k, v in snapshot.items() if k != "version"}}
        try:
            data = await ZoomProvider(conn.mode).create(token, payload, operation)
        except ProviderError as exc:
            if exc.invalid_grant:
                conn.status, conn.tokens_enc = "RECONNECT_REQUIRED", None
            occ.zoom_provisioning_status = "UNCERTAIN" if exc.ambiguous else "FAILED"
            await db.commit()
            raise HTTPException(503, str(exc) + ("; reconcile before retrying" if exc.ambiguous else ""))
        persist_meeting(occ, conn, data)
        effective = data.get("settings", {})
        restricted = [key for key in payload["settings"] if key in effective and effective[key] != payload["settings"][key]]
        occ.zoom_settings_snapshot = dict(snapshot, restricted_settings=restricted, provider_effective=effective)
        await db.commit()
    member = await db.get(OrganizationMember, conn.member_id)
    can_start = bool(is_host and actor_user_id and member.user_id == actor_user_id)
    result = {"join_url": occ.meeting_url, "target_url": occ.meeting_url, "host_url": None,
              "meeting_id": occ.provider_meeting_id, "provider": occ.meeting_provider, "is_simulated": conn.mode == "mock",
              "host_email": occ.zoom_host_email, "can_start": can_start,
              "start_responsibility": "Hosted by " + (occ.zoom_host_email or "the connected account holder") + "; that account holder must start the meeting",
              "restricted_settings": (occ.zoom_settings_snapshot or {}).get("restricted_settings", [])}
    if can_start:
        try:
            token = await access_token(db, conn)
            data = await ZoomProvider(conn.mode).meeting(token, occ.provider_meeting_id)
        except ProviderError as exc:
            if exc.invalid_grant:
                conn.status, conn.tokens_enc = "RECONNECT_REQUIRED", None
                await db.commit()
            raise HTTPException(503, str(exc))
        result["host_url"] = result["target_url"] = data["start_url"]
    await db.commit()
    return result


def persist_meeting(occ, conn, data):
    occ.meeting_url, occ.provider_meeting_id = data["join_url"], str(data["id"])
    occ.meeting_provider = "DEV_SIMULATION" if conn.mode == "mock" else "ZOOM"
    occ.host_url = None  # Host URLs are obtained only for an explicitly authorized host request.
    occ.zoom_provisioning_status = "READY"


async def synchronize(db, occ, action, *, start=None, end=None, topic=None):
    if not occ.provider_meeting_id:
        return
    conn = await host_for_occurrence(db, occ)
    token = await access_token(db, conn)
    provider = ZoomProvider(conn.mode)
    try:
        if action == "cancel":
            await provider.delete(token, occ.provider_meeting_id)
        else:
            await check_conflicts(db, occ, conn, start, end)
            await provider.update(token, occ.provider_meeting_id, {"start_time": ensure_utc(start).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "duration": max(1, int((end - start).total_seconds() / 60)),
                **({"topic": topic} if topic is not None else {})})
    except ProviderError as exc:
        if exc.invalid_grant:
            conn.status, conn.tokens_enc = "RECONNECT_REQUIRED", None
            await db.commit()
        raise HTTPException(503, str(exc) + "; the local class was not changed. Retry this operation.")
