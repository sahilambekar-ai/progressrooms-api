"""Transactional meeting events enqueue Zoom Mail; the worker performs delivery."""
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from sqlalchemy import select, update, or_
from sqlalchemy.orm import selectinload
from app.common.dates import ensure_utc
from app.core.config import settings
from app.models.user import User
from app.models.session import Session, SessionInstructor
from app.models.organization import Organization, OrganizationMember
from app.models.schedule import ClassOccurrence
from app.models.commerce import SessionEnrollment
from app.models.communication import ClassReminderSchedule, PendingMaterialChange, MeetingEmailDelivery, UserCommunicationPreference, PlatformCommunicationSetting

MATERIAL_FIELDS = {"actual_start_at", "actual_end_at", "instructor_id", "meeting_url", "name", "full_description", "duration_minutes",
    "short_description", "requirements", "is_active", "is_public", "skill_level", "session_type", "capacity",
    "min_age", "max_age", "pricing", "instructor_member_ids", "schedule_rules"}
FIELD_LABELS = {"actual_start_at": "start time", "actual_end_at": "end time", "instructor_id": "teacher",
    "instructor_member_ids": "teachers", "schedule_rules": "timetable", "is_active": "class availability",
    "is_public": "public visibility", "requirements": "equipment and preparation", "pricing": "pricing and packages"}


async def record_session_edit(db, session_id, changed_fields):
    material = {key: value for key, value in changed_fields.items() if key in MATERIAL_FIELDS}
    if not material:
        return
    await db.flush()
    occurrences = (await db.scalars(select(ClassOccurrence).where(
        ClassOccurrence.session_id == session_id, ClassOccurrence.actual_end_at > datetime.now(timezone.utc),
        ClassOccurrence.status.in_(["SCHEDULED", "RESCHEDULED", "IN_PROGRESS"])).with_for_update())).all()
    for occurrence in occurrences:
        await record_class_edit(db, occurrence.id, material, {})


def format_ist_time(dt):
    return ensure_utc(dt).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%I:%M %p IST, %b %d")


def calculate_suggested_join_time(start_dt):
    return format_ist_time(start_dt - timedelta(minutes=5))


async def recipients_for(db, occ):
    """Only enrollments valid for this occurrence, plus its assigned active teachers."""
    users = list((await db.scalars(select(User).join(SessionEnrollment, SessionEnrollment.user_id == User.id).where(
        SessionEnrollment.session_id == occ.session_id,
        SessionEnrollment.organization_id == occ.organization_id,
        SessionEnrollment.status == "ACTIVE", SessionEnrollment.valid_from <= occ.actual_start_at,
        or_(SessionEnrollment.valid_until.is_(None), SessionEnrollment.valid_until >= occ.actual_start_at),
        or_(SessionEnrollment.total_sessions.is_(None), SessionEnrollment.sessions_attended < SessionEnrollment.total_sessions,
            (SessionEnrollment.purchase_order_id.is_(None) & SessionEnrollment.session.has(Session.session_type == "ONE_TO_ONE"))),
        User.is_active.is_(True)))).all())
    teachers = select(User).join(OrganizationMember, OrganizationMember.user_id == User.id).where(
        OrganizationMember.organization_id == occ.organization_id, OrganizationMember.is_active.is_(True), User.is_active.is_(True))
    if occ.instructor_id:
        teachers = teachers.where(OrganizationMember.id == occ.instructor_id)
    else:
        teachers = teachers.where(OrganizationMember.id.in_(select(SessionInstructor.member_id).where(SessionInstructor.session_id == occ.session_id)))
    users.extend((await db.scalars(teachers)).all())
    return list({user.id: user for user in users if user.email}.values())


async def queue_meeting_email(db, occ, message_type, event_key, *, recipient_id=None, exclude_recipient_ids=(), reason=None, current_time=None):
    """Caller holds the occurrence lock. Enqueue inside the class-edit transaction."""
    now = current_time or datetime.now(timezone.utc)
    recipients = await recipients_for(db, occ)
    if recipient_id:
        recipients = [user for user in recipients if user.id == recipient_id]
    existing = set((await db.scalars(select(MeetingEmailDelivery.recipient_user_id).where(
        MeetingEmailDelivery.event_key == event_key))).all())
    count = 0
    for user in recipients:
        if user.id in existing or user.id in exclude_recipient_ids:
            continue
        db.add(MeetingEmailDelivery(class_occurrence_id=occ.id, recipient_user_id=user.id,
            event_key=event_key, message_type=message_type, scheduled_start_at=occ.actual_start_at,
            available_at=now, reason=reason, status="PENDING"))
        count += 1
    await db.flush()
    return count


async def queue_generated_invites(db, occurrences):
    """New occurrences have no outbox rows yet; resolve their recipients in one query."""
    if not occurrences:
        return 0
    now = datetime.now(timezone.utc)
    by_id = {o.id: o for o in occurrences if ensure_utc(o.actual_start_at) > now}
    if not by_id:
        return 0
    students = select(ClassOccurrence.id, User.id).select_from(ClassOccurrence).join(
        SessionEnrollment, SessionEnrollment.session_id == ClassOccurrence.session_id).join(
        User, User.id == SessionEnrollment.user_id).where(
        ClassOccurrence.id.in_(by_id), User.is_active.is_(True),
        SessionEnrollment.organization_id == ClassOccurrence.organization_id,
        SessionEnrollment.status == "ACTIVE", SessionEnrollment.valid_from <= ClassOccurrence.actual_start_at,
        or_(SessionEnrollment.valid_until.is_(None), SessionEnrollment.valid_until >= ClassOccurrence.actual_start_at),
        or_(SessionEnrollment.total_sessions.is_(None), SessionEnrollment.sessions_attended < SessionEnrollment.total_sessions))
    teachers = select(ClassOccurrence.id, User.id).select_from(ClassOccurrence).join(
        OrganizationMember, OrganizationMember.id == ClassOccurrence.instructor_id).join(
        User, User.id == OrganizationMember.user_id).where(ClassOccurrence.id.in_(by_id),
        OrganizationMember.organization_id == ClassOccurrence.organization_id,
        OrganizationMember.is_active.is_(True), User.is_active.is_(True))
    recipients = set((await db.execute(students.union(teachers))).all())
    for occurrence_id, user_id in recipients:
        occ = by_id[occurrence_id]
        db.add(MeetingEmailDelivery(class_occurrence_id=occ.id, recipient_user_id=user_id,
            event_key=f"created:{occ.id}", message_type="CLASS_INVITE", scheduled_start_at=occ.actual_start_at,
            available_at=now, status="PENDING"))
    await db.flush()
    return len(recipients)


async def sync_reminder_schedules_for_occurrence(db, occurrence_id, new_start_at, schedule_version=1, current_time=None):
    now = ensure_utc(current_time or datetime.now(timezone.utc))
    start = ensure_utc(new_start_at)
    await db.execute(update(ClassReminderSchedule).where(ClassReminderSchedule.class_occurrence_id == occurrence_id,
        ClassReminderSchedule.status == "PENDING").values(status="SUPERSEDED"))
    await db.execute(update(MeetingEmailDelivery).where(MeetingEmailDelivery.class_occurrence_id == occurrence_id,
        MeetingEmailDelivery.message_type.in_(["REMINDER_30M", "CLASS_INVITE"]),
        MeetingEmailDelivery.status.in_(["PENDING", "RETRY"])).values(status="SUPERSEDED"))
    if start <= now:
        return []
    job = ClassReminderSchedule(class_occurrence_id=occurrence_id, schedule_version=schedule_version,
        reminder_type="REMINDER_30M", target_time=max(now, start - timedelta(minutes=30)), status="PENDING", details_snapshot={"start_at": start.isoformat()})
    db.add(job)
    await db.flush()
    return [job]


async def record_class_edit(db, occurrence_id, changed_fields, old_values, current_time=None):
    now = ensure_utc(current_time or datetime.now(timezone.utc))
    occ = await db.scalar(select(ClassOccurrence).where(ClassOccurrence.id == occurrence_id).with_for_update())
    if not occ:
        return {"action": "NOT_FOUND", "notifications_sent": 0}
    material = {key: value for key, value in changed_fields.items() if key in MATERIAL_FIELDS}
    if not material:
        return {"action": "NON_MATERIAL_IGNORED", "notifications_sent": 0}
    if "actual_start_at" in material:
        await sync_reminder_schedules_for_occurrence(db, occurrence_id, material["actual_start_at"], current_time=now)
    change = await db.scalar(select(PendingMaterialChange).where(
        PendingMaterialChange.class_occurrence_id == occurrence_id, PendingMaterialChange.processed.is_(False)))
    summary = {key: str(value) for key, value in material.items()}
    if change:
        change.changes_summary = dict(change.changes_summary, **summary)
    else:
        change = PendingMaterialChange(class_occurrence_id=occurrence_id, first_change_at=now,
            debounce_until=now + timedelta(seconds=120), changes_summary=summary, processed=False)
        db.add(change)
    new_start = material.get("actual_start_at")
    if new_start and old_values.get("actual_start_at") and ensure_utc(new_start) < ensure_utc(old_values["actual_start_at"]):
        change.debounce_until = now
    await db.flush()
    return {"action": "CORRECTION_QUEUED", "notifications_sent": 0}


async def handle_class_cancellation(db, occurrence_id, reason=None, current_time=None):
    now = current_time or datetime.now(timezone.utc)
    occ = await db.scalar(select(ClassOccurrence).where(ClassOccurrence.id == occurrence_id).with_for_update())
    if not occ:
        return 0
    await db.execute(update(ClassReminderSchedule).where(ClassReminderSchedule.class_occurrence_id == occurrence_id,
        ClassReminderSchedule.status == "PENDING").values(status="CANCELLED"))
    await db.execute(update(MeetingEmailDelivery).where(MeetingEmailDelivery.class_occurrence_id == occurrence_id,
        MeetingEmailDelivery.status.in_(["PENDING", "RETRY"])).values(status="CANCELLED"))
    await db.execute(update(PendingMaterialChange).where(PendingMaterialChange.class_occurrence_id == occurrence_id,
        PendingMaterialChange.processed.is_(False)).values(processed=True))
    return await queue_meeting_email(db, occ, "CLASS_CANCELLATION", f"cancel:{occ.id}", reason=reason, current_time=now)


async def backfill_reminders(db, now, organization_id=None):
    """Catch existing and newly booked occurrences without replaying legacy reminders."""
    rows = (await db.scalars(select(ClassOccurrence).join(Session, Session.id == ClassOccurrence.session_id).where(
        ClassOccurrence.actual_start_at > now, ClassOccurrence.actual_start_at <= now + timedelta(days=7),
        *([ClassOccurrence.organization_id == organization_id] if organization_id else []),
        ClassOccurrence.status.in_(["SCHEDULED", "RESCHEDULED"]), or_(Session.is_active.is_(True), Session.session_type == "ONE_TO_ONE"), Session.deleted_at.is_(None),
        ~select(ClassReminderSchedule.id).where(ClassReminderSchedule.class_occurrence_id == ClassOccurrence.id,
            ClassReminderSchedule.reminder_type == "REMINDER_30M", ClassReminderSchedule.status != "SUPERSEDED").exists()
    ).order_by(ClassOccurrence.actual_start_at).limit(100).with_for_update(skip_locked=True, of=ClassOccurrence))).all()
    for occ in rows:
        await sync_reminder_schedules_for_occurrence(db, occ.id, occ.actual_start_at, current_time=now)
    await db.flush()


async def dispatch_pending_reminders(db, as_of_time=None, occurrence_id=None, organization_id=None):
    """Expand due jobs into per-recipient queue records; no network calls here."""
    now = ensure_utc(as_of_time or datetime.now(timezone.utc))
    stmt = select(ClassReminderSchedule).where(ClassReminderSchedule.status == "PENDING", ClassReminderSchedule.target_time <= now)
    if occurrence_id:
        stmt = stmt.where(ClassReminderSchedule.class_occurrence_id == occurrence_id)
    if organization_id:
        stmt = stmt.where(ClassReminderSchedule.class_occurrence_id.in_(select(ClassOccurrence.id).where(ClassOccurrence.organization_id == organization_id)))
    jobs = (await db.scalars(stmt.order_by(ClassReminderSchedule.target_time).limit(100).with_for_update(skip_locked=True))).all()
    count = 0
    for job in jobs:
        occ = await db.scalar(select(ClassOccurrence).where(ClassOccurrence.id == job.class_occurrence_id).with_for_update())
        session = await db.get(Session, occ.session_id) if occ else None
        if not occ or not session or occ.status == "CANCELLED" or (not session.is_active and session.session_type != "ONE_TO_ONE") or session.deleted_at:
            job.status = "CANCELLED"
        elif job.reminder_type != "REMINDER_30M":
            job.status = "SUPERSEDED"
        elif ensure_utc(occ.actual_start_at) <= now:
            job.status = "EXPIRED"
        elif (job.details_snapshot or {}).get("start_at") != ensure_utc(occ.actual_start_at).isoformat():
            job.status = "SUPERSEDED"
        else:
            key = f"reminder:{occ.id}:{ensure_utc(occ.actual_start_at).isoformat()}"
            count += await queue_meeting_email(db, occ, "REMINDER_30M", key, current_time=now)
            job.status = "QUEUED"
    await db.flush()
    return count


async def process_debounced_material_corrections(db, as_of_time=None, occurrence_id=None, organization_id=None):
    now = as_of_time or datetime.now(timezone.utc)
    stmt = select(PendingMaterialChange).where(PendingMaterialChange.processed.is_(False), PendingMaterialChange.debounce_until <= now)
    if occurrence_id:
        stmt = stmt.where(PendingMaterialChange.class_occurrence_id == occurrence_id)
    if organization_id:
        stmt = stmt.where(PendingMaterialChange.class_occurrence_id.in_(select(ClassOccurrence.id).where(ClassOccurrence.organization_id == organization_id)))
    changes = (await db.scalars(stmt.limit(100).with_for_update(skip_locked=True))).all()
    count = 0
    for change in changes:
        occ = await db.scalar(select(ClassOccurrence).where(ClassOccurrence.id == change.class_occurrence_id).with_for_update())
        if occ and occ.status != "CANCELLED" and ensure_utc(occ.actual_end_at) > ensure_utc(now):
            count += await queue_meeting_email(db, occ, "CLASS_CORRECTION", f"change:{change.id}",
                reason="Updated: " + ", ".join(FIELD_LABELS.get(key, key.replace("_", " ")) for key in sorted(change.changes_summary)) + ". Please check the latest class details.", current_time=now)
        change.processed = True
    await db.flush()
    return count


async def render_meeting_email(db, occ, user, kind, reason=None):
    session = await db.get(Session, occ.session_id)
    org = await db.get(Organization, occ.organization_id)
    zone = ZoneInfo(org.timezone or "Asia/Kolkata")
    start = ensure_utc(occ.actual_start_at).astimezone(zone).strftime("%A, %d %b %Y, %I:%M %p %Z")
    end = ensure_utc(occ.actual_end_at).astimezone(zone).strftime("%I:%M %p %Z")
    titles = {"REMINDER_30M": "Class reminder", "CLASS_INVITE": "Class invitation", "CLASS_CORRECTION": "Class updated", "CLASS_CANCELLATION": "Class cancelled"}
    subject = f"{titles[kind]}: {session.name} — {org.name}"
    body = f"Hello {user.full_name},\n\n{titles[kind]}\n\nClass: {session.name}\nStudio: {org.name}\nStart: {start}\nEnd: {end}\n"
    if kind == "CLASS_CORRECTION":
        body += f"Status: {'Assigned appointment' if session.session_type == 'ONE_TO_ONE' else 'Active' if session.is_active else 'Paused — do not attend until the studio resumes this class'}\n"
    if kind == "REMINDER_30M":
        body += "\nYour class starts soon. This reminder is scheduled 30 minutes before class.\n"
    if reason:
        body += f"\n{reason}\n"
    if kind != "CLASS_CANCELLATION" and (session.is_active or session.session_type == "ONE_TO_ONE"):
        # Role-aware authenticated access links never include private Zoom host credentials.
        query = urlencode({"studio": str(org.id)})
        body += f"\nOpen your class and join: {settings.WEBSITE_PUBLIC_ORIGIN.rstrip('/')}/meetings/{occ.id}?{query}\n"

    return subject, body


async def request_meeting_invites(db, occ, recipient_id=None):
    from fastapi import HTTPException
    now = datetime.now(timezone.utc)
    session = await db.get(Session, occ.session_id)
    if occ.status == "CANCELLED" or not session or (not session.is_active and session.session_type != "ONE_TO_ONE") or session.deleted_at or ensure_utc(occ.actual_end_at) <= now:
        raise HTTPException(409, "Invitations can only be sent for an active, unfinished class")
    recipients = await recipients_for(db, occ)
    if recipient_id and recipient_id not in {user.id for user in recipients}:
        raise HTTPException(403, "You do not have an eligible enrollment for this class")
    channels = await db.scalar(select(PlatformCommunicationSetting).limit(1))
    if channels and not channels.email_enabled:
        raise HTTPException(409, "Email notifications are currently paused")
    opted_out = set((await db.scalars(select(UserCommunicationPreference.user_id).where(
        UserCommunicationPreference.user_id.in_([u.id for u in recipients if not recipient_id or u.id == recipient_id]),
        UserCommunicationPreference.email_opted_out.is_(True)))).all())
    if not any(u.id not in opted_out for u in recipients if not recipient_id or u.id == recipient_id):
        raise HTTPException(409, "No recipients have class email enabled. Check recipient notification preferences before retrying.")
    from app.modules.zoom.meetings import host_for_occurrence
    from app.modules.communication.zoom_mail import SEND_SCOPES
    conn = await host_for_occurrence(db, occ)
    if conn.mode != "mock" and not SEND_SCOPES.intersection((conn.scopes or "").split()):
        raise HTTPException(409, "The class host must authorize Zoom Mail before invitations can be sent")
    # A one-minute cooldown makes double clicks/retries reuse the existing request.
    recent = select(MeetingEmailDelivery).where(MeetingEmailDelivery.class_occurrence_id == occ.id,
        MeetingEmailDelivery.message_type == "CLASS_INVITE", MeetingEmailDelivery.created_at >= now - timedelta(minutes=1),
        MeetingEmailDelivery.status.notin_(["CANCELLED", "SUPERSEDED", "FAILED", "SUPPRESSED"]))
    if recipient_id:
        recent = recent.where(MeetingEmailDelivery.recipient_user_id == recipient_id)
    existing = (await db.scalars(recent)).all()
    addressed = {row.recipient_user_id for row in existing}
    intended = {user.id for user in recipients if user.id not in opted_out and (not recipient_id or user.id == recipient_id)}
    if intended and intended.issubset(addressed):
        return {"status": "QUEUED", "recipients": len(intended), "message": "An invitation was already requested. Check your email shortly."}
    count = await queue_meeting_email(db, occ, "CLASS_INVITE", f"invite:{occ.id}:{uuid.uuid4()}",
        recipient_id=recipient_id, exclude_recipient_ids=addressed | opted_out)
    if not count:
        raise HTTPException(409, "No eligible recipients have an email address")
    await db.commit()
    return {"status": "QUEUED", "recipients": count, "message": f"Invitation queued for {count} recipient{'s' if count != 1 else ''} through Zoom Mail."}
