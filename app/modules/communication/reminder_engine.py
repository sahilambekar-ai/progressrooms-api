import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from sqlalchemy import select, update, and_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.organization import Organization, OrganizationMember
from app.models.session import Session
from app.models.schedule import ClassOccurrence
from app.models.commerce import SessionEnrollment
from app.models.communication import (
    PlatformCommunicationSetting,
    ClassReminderSchedule,
    PendingMaterialChange,
    CommunicationLog,
    UserCommunicationPreference,
)
from app.modules.communication.service import get_or_create_settings, check_and_increment_studio_budget
from app.modules.communication.templates import (
    render_class_reminder_2h_wa,
    render_class_reminder_15m_wa,
    render_class_correction_wa,
    render_class_cancellation_wa,
    render_class_reminder_2h_email,
    render_class_reminder_15m_email,
    render_class_correction_email,
    render_class_cancellation_email,
)
from app.core.config import settings

logger = logging.getLogger("progressrooms.reminder_engine")

MATERIAL_FIELDS = {"actual_start_at", "actual_end_at", "instructor_id", "meeting_url"}


def format_ist_time(dt: datetime) -> str:
    # India Standard Time UTC+5:30
    ist_offset = timedelta(hours=5, minutes=30)
    ist_dt = dt.astimezone(timezone(ist_offset)) if dt.tzinfo else (dt + ist_offset)
    return ist_dt.strftime("%I:%M %p IST, %b %d")


def calculate_suggested_join_time(start_dt: datetime) -> str:
    # 5 minutes prior
    return format_ist_time(start_dt - timedelta(minutes=5))


async def sync_reminder_schedules_for_occurrence(
    db: AsyncSession,
    occurrence_id: uuid.UUID,
    new_start_at: datetime,
    schedule_version: int = 1,
    current_time: Optional[datetime] = None
) -> List[ClassReminderSchedule]:
    """
    Recalculates reminder jobs against the occurrence's current start time.
    Suppresses/supersedes jobs belonging to previous schedule versions.
    Never sends a backlog of missed reminders.
    """
    now = current_time or datetime.now(timezone.utc)

    # 1. Supersede any existing pending jobs
    await db.execute(
        update(ClassReminderSchedule)
        .where(
            ClassReminderSchedule.class_occurrence_id == occurrence_id,
            ClassReminderSchedule.status == "PENDING"
        )
        .values(status="SUPERSEDED")
    )

    created_jobs: List[ClassReminderSchedule] = []

    target_2h = new_start_at - timedelta(hours=2)
    target_15m = new_start_at - timedelta(minutes=15)

    # 2-hour reminder
    if now < target_2h:
        job_2h = ClassReminderSchedule(
            class_occurrence_id=occurrence_id,
            schedule_version=schedule_version,
            reminder_type="REMINDER_2H",
            target_time=target_2h,
            status="PENDING"
        )
        db.add(job_2h)
        created_jobs.append(job_2h)
    elif target_2h <= now < target_15m:
        # Created or rescheduled within 2 hours: send 1 immediate preparation reminder
        job_immediate = ClassReminderSchedule(
            class_occurrence_id=occurrence_id,
            schedule_version=schedule_version,
            reminder_type="REMINDER_2H",
            target_time=now,
            status="PENDING"
        )
        db.add(job_immediate)
        created_jobs.append(job_immediate)

    # 15-minute reminder
    if now < target_15m:
        job_15m = ClassReminderSchedule(
            class_occurrence_id=occurrence_id,
            schedule_version=schedule_version,
            reminder_type="REMINDER_15M",
            target_time=target_15m,
            status="PENDING"
        )
        db.add(job_15m)
        created_jobs.append(job_15m)
    elif target_15m <= now < new_start_at:
        # Created or rescheduled within 15 minutes: send 1 immediate starting soon notice
        job_imm_15 = ClassReminderSchedule(
            class_occurrence_id=occurrence_id,
            schedule_version=schedule_version,
            reminder_type="REMINDER_15M",
            target_time=now,
            status="PENDING"
        )
        db.add(job_imm_15)
        created_jobs.append(job_imm_15)

    await db.flush()
    return created_jobs


async def record_class_edit(
    db: AsyncSession,
    occurrence_id: uuid.UUID,
    changed_fields: Dict[str, Any],
    old_values: Dict[str, Any],
    current_time: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Evaluates class modifications according to the product specification:
    - Edits before T-2h: Silent update. Reminders will include final state. No notifications sent.
    - Material edits after a reminder has been sent: Consolidate with a 2-minute debounce window.
    - Non-material edits (description, cover photo): No standalone notifications.
    - Class moved earlier where waiting risks missing: Notify promptly.
    """
    now = current_time or datetime.now(timezone.utc)

    # Check if a reminder has ALREADY been sent for this occurrence
    has_sent_reminder = await db.scalar(
        select(ClassReminderSchedule.id).where(
            ClassReminderSchedule.class_occurrence_id == occurrence_id,
            ClassReminderSchedule.status == "SENT"
        ).limit(1)
    ) is not None

    material_changes = {k: v for k, v in changed_fields.items() if k in MATERIAL_FIELDS}

    if not has_sent_reminder:
        # Edits before first reminder: Routine changes update the application without sending messages.
        # If start time changed, recalculate pending reminder jobs against new start time
        if "actual_start_at" in changed_fields:
            await sync_reminder_schedules_for_occurrence(
                db=db,
                occurrence_id=occurrence_id,
                new_start_at=changed_fields["actual_start_at"],
                current_time=now
            )
        return {
            "action": "SILENT_UPDATE",
            "message": "Routine edit saved silently before reminder window. Final details will be included in the scheduled reminder.",
            "notifications_sent": 0
        }

    # If reminder was already sent, check for material change
    if not material_changes:
        return {
            "action": "NON_MATERIAL_IGNORED",
            "message": "Non-material edit (description/notes/images) saved without standalone notification.",
            "notifications_sent": 0
        }

    # Material change after reminder was sent: Consolidate in 2-minute debounce window
    old_start = old_values.get("actual_start_at")
    new_start = changed_fields.get("actual_start_at")
    moved_earlier = False
    if old_start and new_start and new_start < old_start:
        moved_earlier = True

    # If moved earlier and class starts in < 2 hours, expedite correction immediately
    expedite = moved_earlier and ((new_start - now).total_seconds() < 7200)
    debounce_delta = timedelta(seconds=0 if expedite else 120)

    # Upsert into PendingMaterialChange
    stmt = select(PendingMaterialChange).where(
        PendingMaterialChange.class_occurrence_id == occurrence_id,
        PendingMaterialChange.processed == False
    )
    existing_change = (await db.execute(stmt)).scalar_one_or_none()

    if existing_change:
        # Merge changes
        merged = dict(existing_change.changes_summary)
        merged.update({k: str(v) for k, v in material_changes.items()})
        existing_change.changes_summary = merged
        if expedite:
            existing_change.debounce_until = now
    else:
        new_change = PendingMaterialChange(
            class_occurrence_id=occurrence_id,
            first_change_at=now,
            debounce_until=now + debounce_delta,
            changes_summary={k: str(v) for k, v in material_changes.items()},
            processed=False
        )
        db.add(new_change)

    # If start time changed, update subsequent reminder jobs
    if new_start:
        await sync_reminder_schedules_for_occurrence(
            db=db,
            occurrence_id=occurrence_id,
            new_start_at=new_start,
            current_time=now
        )

    await db.flush()
    return {
        "action": "CORRECTION_QUEUED",
        "expedited": expedite,
        "message": "Material edit queued for consolidated correction notification." if not expedite else "Expedited material correction queued (moved earlier).",
        "notifications_sent": 0
    }


async def handle_class_cancellation(
    db: AsyncSession,
    occurrence_id: uuid.UUID,
    reason: Optional[str] = None,
    current_time: Optional[datetime] = None
) -> int:
    """
    Cancels all pending reminders for this class and immediately dispatches cancellation notices
    to enrolled students and assigned teachers.
    """
    now = current_time or datetime.now(timezone.utc)

    # 1. Cancel pending reminder jobs
    await db.execute(
        update(ClassReminderSchedule)
        .where(
            ClassReminderSchedule.class_occurrence_id == occurrence_id,
            ClassReminderSchedule.status == "PENDING"
        )
        .values(status="CANCELLED")
    )

    # 2. Fetch occurrence, session, studio, and enrolled students
    stmt = (
        select(ClassOccurrence)
        .options(
            selectinload(ClassOccurrence.session),
            selectinload(ClassOccurrence.instructor).selectinload(OrganizationMember.user),
            selectinload(ClassOccurrence.session).selectinload(Session.organization),
            selectinload(ClassOccurrence.session).selectinload(Session.enrollments).selectinload(SessionEnrollment.user),
        )
        .where(ClassOccurrence.id == occurrence_id)
    )
    occ = (await db.execute(stmt)).scalar_one_or_none()
    if not occ or not occ.session:
        return 0

    studio_name = occ.session.organization.name if occ.session.organization else "Studio"
    class_name = occ.session.name
    time_ist = format_ist_time(occ.actual_start_at)

    settings_summary = await get_or_create_settings(db)
    notifications_sent = 0

    # Collect recipient users (enrolled active students + assigned teacher)
    recipients: List[User] = []
    if occ.session.enrollments:
        for enr in occ.session.enrollments:
            if enr.status == "ACTIVE" and enr.user and enr.user not in recipients:
                recipients.append(enr.user)

    if occ.instructor and occ.instructor.user and occ.instructor.user not in recipients:
        recipients.append(occ.instructor.user)

    for user in recipients:
        # Check user communication preferences
        pref = await db.scalar(
            select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == user.id)
        )

        # A. WhatsApp cancellation
        wa_target = user.whatsapp_number or user.phone
        if wa_target:
            if not settings_summary.whatsapp_enabled:
                db.add(CommunicationLog(
                    recipient_user_id=user.id,
                    organization_id=occ.organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_CLASSES",
                    message_type="CLASS_CANCELLATION",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: whatsapp disabled",
                    subject_or_title=f"🛑 Cancelled: {class_name}",
                    metadata_json={"occurrence_id": str(occ.id), "reason": reason}
                ))
            elif pref and pref.whatsapp_opted_out:
                db.add(CommunicationLog(
                    recipient_user_id=user.id,
                    organization_id=occ.organization_id,
                    recipient_phone=wa_target,
                    channel="WHATSAPP_CLASSES",
                    message_type="CLASS_CANCELLATION",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: recipient opt-out",
                    subject_or_title=f"🛑 Cancelled: {class_name}"
                ))
            else:
                budget_ok, budget_reason = await check_and_increment_studio_budget(
                    db=db,
                    organization_id=occ.organization_id,
                    channel="WHATSAPP_CLASSES",
                    message_type="CLASS_CANCELLATION"
                )
                if not budget_ok:
                    db.add(CommunicationLog(
                        recipient_user_id=user.id,
                        organization_id=occ.organization_id,
                        recipient_phone=wa_target,
                        channel="WHATSAPP_CLASSES",
                        message_type="CLASS_CANCELLATION",
                        status="SUPPRESSED",
                        suppression_reason=budget_reason,
                        subject_or_title=f"🛑 Cancelled: {class_name}",
                        metadata_json={"occurrence_id": str(occ.id), "reason": reason}
                    ))
                else:
                    wa_body = render_class_cancellation_wa(studio_name, class_name, time_ist, reason)
                    db.add(CommunicationLog(
                        recipient_user_id=user.id,
                        organization_id=occ.organization_id,
                        recipient_phone=wa_target,
                        channel="WHATSAPP_CLASSES",
                        message_type="CLASS_CANCELLATION",
                        status="SENT",
                        subject_or_title=f"🛑 Cancelled: {class_name}",
                        body_preview=wa_body[:200],
                        metadata_json={"occurrence_id": str(occ.id), "reason": reason}
                    ))
                    notifications_sent += 1

        # B. Email cancellation
        if user.email:
            if not settings_summary.email_enabled:
                db.add(CommunicationLog(
                    recipient_user_id=user.id,
                    organization_id=occ.organization_id,
                    recipient_email=user.email,
                    channel="EMAIL",
                    message_type="CLASS_CANCELLATION",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: email disabled",
                    subject_or_title=f"🛑 Cancelled: {class_name}"
                ))
            elif pref and pref.email_opted_out:
                db.add(CommunicationLog(
                    recipient_user_id=user.id,
                    organization_id=occ.organization_id,
                    recipient_email=user.email,
                    channel="EMAIL",
                    message_type="CLASS_CANCELLATION",
                    status="SUPPRESSED",
                    suppression_reason="Suppressed: recipient opt-out",
                    subject_or_title=f"🛑 Cancelled: {class_name}"
                ))
            else:
                budget_ok, budget_reason = await check_and_increment_studio_budget(
                    db=db,
                    organization_id=occ.organization_id,
                    channel="EMAIL",
                    message_type="CLASS_CANCELLATION"
                )
                if not budget_ok:
                    db.add(CommunicationLog(
                        recipient_user_id=user.id,
                        organization_id=occ.organization_id,
                        recipient_email=user.email,
                        channel="EMAIL",
                        message_type="CLASS_CANCELLATION",
                        status="SUPPRESSED",
                        suppression_reason=budget_reason,
                        subject_or_title=f"🛑 Cancelled: {class_name}",
                        metadata_json={"occurrence_id": str(occ.id), "reason": reason}
                    ))
                else:
                    subj, _ = render_class_cancellation_email(user.full_name, studio_name, class_name, time_ist, reason)
                    db.add(CommunicationLog(
                        recipient_user_id=user.id,
                        organization_id=occ.organization_id,
                        recipient_email=user.email,
                        channel="EMAIL",
                        message_type="CLASS_CANCELLATION",
                        status="SENT",
                        subject_or_title=subj,
                        body_preview=f"Class cancelled: {class_name} at {studio_name}",
                        metadata_json={"occurrence_id": str(occ.id), "reason": reason}
                    ))
                    notifications_sent += 1


    await db.commit()
    return notifications_sent


async def dispatch_pending_reminders(
    db: AsyncSession,
    as_of_time: Optional[datetime] = None,
    occurrence_id: Optional[uuid.UUID] = None
) -> int:
    """
    Finds and dispatches due ClassReminderSchedules (target_time <= as_of_time).
    Enforces backend channel switches (WhatsApp & Email).
    Uses the latest details at dispatch time.
    """
    now = as_of_time or datetime.now(timezone.utc)
    settings_summary = await get_or_create_settings(db)

    stmt = (
        select(ClassReminderSchedule)
        .options(
            selectinload(ClassReminderSchedule.occurrence)
            .selectinload(ClassOccurrence.session)
            .selectinload(Session.organization),
            selectinload(ClassReminderSchedule.occurrence)
            .selectinload(ClassOccurrence.instructor)
            .selectinload(OrganizationMember.user),
            selectinload(ClassReminderSchedule.occurrence)
            .selectinload(ClassOccurrence.session)
            .selectinload(Session.enrollments)
            .selectinload(SessionEnrollment.user),
        )
        .where(
            ClassReminderSchedule.status == "PENDING",
            ClassReminderSchedule.target_time <= now
        )
    )
    if occurrence_id:
        stmt = stmt.where(ClassReminderSchedule.class_occurrence_id == occurrence_id)

    result = await db.execute(stmt)
    due_jobs = result.scalars().all()

    total_dispatched = 0

    for job in due_jobs:
        occ = job.occurrence
        if not occ or not occ.session or occ.status == "CANCELLED":
            job.status = "CANCELLED"
            continue

        from app.modules.billing.saas import operational_access
        if not await operational_access(db, occ.organization_id):
            continue

        studio_name = occ.session.organization.name if occ.session.organization else "Studio"
        class_name = occ.session.name
        teacher_name = occ.instructor.user.full_name if (occ.instructor and occ.instructor.user) else "Assigned Instructor"
        time_ist = format_ist_time(occ.actual_start_at)
        join_early_ist = calculate_suggested_join_time(occ.actual_start_at)
        duration_mins = occ.session.duration_minutes or 60

        # Secure portal link: checks student enrollment upon click
        public_origin = getattr(settings, "WEBSITE_PUBLIC_ORIGIN", "http://localhost:3000")
        join_url = f"{public_origin}/student?studio={occ.organization_id}&class={occ.session_id}&occ={occ.id}"

        # Collect recipients
        recipients: List[User] = []
        if occ.session.enrollments:
            for enr in occ.session.enrollments:
                if enr.status == "ACTIVE" and enr.user and enr.user not in recipients:
                    recipients.append(enr.user)
        if occ.instructor and occ.instructor.user and occ.instructor.user not in recipients:
            recipients.append(occ.instructor.user)

        job_sent = False

        for user in recipients:
            pref = await db.scalar(
                select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == user.id)
            )

            # 1. WhatsApp send
            wa_target = user.whatsapp_number or user.phone
            if wa_target:
                if not settings_summary.whatsapp_enabled:
                    db.add(CommunicationLog(
                        recipient_user_id=user.id,
                        organization_id=occ.organization_id,
                        recipient_phone=wa_target,
                        channel="WHATSAPP_CLASSES",
                        message_type=job.reminder_type,
                        status="SUPPRESSED",
                        suppression_reason="Suppressed: whatsapp disabled",
                        subject_or_title=f"Reminder: {class_name}",
                        metadata_json={"occurrence_id": str(occ.id), "schedule_version": job.schedule_version}
                    ))
                elif pref and pref.whatsapp_opted_out:
                    db.add(CommunicationLog(
                        recipient_user_id=user.id,
                        organization_id=occ.organization_id,
                        recipient_phone=wa_target,
                        channel="WHATSAPP_CLASSES",
                        message_type=job.reminder_type,
                        status="SUPPRESSED",
                        suppression_reason="Suppressed: recipient opt-out",
                        subject_or_title=f"Reminder: {class_name}"
                    ))
                else:
                    budget_ok, budget_reason = await check_and_increment_studio_budget(
                        db=db,
                        organization_id=occ.organization_id,
                        channel="WHATSAPP_CLASSES",
                        message_type=job.reminder_type
                    )
                    if not budget_ok:
                        db.add(CommunicationLog(
                            recipient_user_id=user.id,
                            organization_id=occ.organization_id,
                            recipient_phone=wa_target,
                            channel="WHATSAPP_CLASSES",
                            message_type=job.reminder_type,
                            status="SUPPRESSED",
                            suppression_reason=budget_reason,
                            subject_or_title=f"Reminder: {class_name}",
                            metadata_json={"occurrence_id": str(occ.id), "schedule_version": job.schedule_version}
                        ))
                    else:
                        if job.reminder_type == "REMINDER_2H":
                            wa_body = render_class_reminder_2h_wa(
                                studio_name=studio_name,
                                class_name=class_name,
                                teacher_name=teacher_name,
                                start_time_ist=time_ist,
                                suggested_join_time_ist=join_early_ist,
                                join_url=join_url
                            )
                        else:
                            wa_body = render_class_reminder_15m_wa(
                                studio_name=studio_name,
                                class_name=class_name,
                                teacher_name=teacher_name,
                                start_time_ist=time_ist,
                                suggested_join_time_ist=join_early_ist,
                                join_url=join_url
                            )

                        db.add(CommunicationLog(
                            recipient_user_id=user.id,
                            organization_id=occ.organization_id,
                            recipient_phone=wa_target,
                            channel="WHATSAPP_CLASSES",
                            message_type=job.reminder_type,
                            status="SENT",
                            subject_or_title=f"Reminder: {class_name}",
                            body_preview=wa_body[:200],
                            metadata_json={"occurrence_id": str(occ.id), "schedule_version": job.schedule_version}
                        ))
                        job_sent = True
                        total_dispatched += 1

            # 2. Email send
            if user.email:
                if not settings_summary.email_enabled:
                    db.add(CommunicationLog(
                        recipient_user_id=user.id,
                        organization_id=occ.organization_id,
                        recipient_email=user.email,
                        channel="EMAIL",
                        message_type=job.reminder_type,
                        status="SUPPRESSED",
                        suppression_reason="Suppressed: email disabled",
                        subject_or_title=f"Reminder: {class_name}",
                        metadata_json={"occurrence_id": str(occ.id), "schedule_version": job.schedule_version}
                    ))
                elif pref and pref.email_opted_out:
                    db.add(CommunicationLog(
                        recipient_user_id=user.id,
                        organization_id=occ.organization_id,
                        recipient_email=user.email,
                        channel="EMAIL",
                        message_type=job.reminder_type,
                        status="SUPPRESSED",
                        suppression_reason="Suppressed: recipient opt-out",
                        subject_or_title=f"Reminder: {class_name}"
                    ))
                else:
                    budget_ok, budget_reason = await check_and_increment_studio_budget(
                        db=db,
                        organization_id=occ.organization_id,
                        channel="EMAIL",
                        message_type=job.reminder_type
                    )
                    if not budget_ok:
                        db.add(CommunicationLog(
                            recipient_user_id=user.id,
                            organization_id=occ.organization_id,
                            recipient_email=user.email,
                            channel="EMAIL",
                            message_type=job.reminder_type,
                            status="SUPPRESSED",
                            suppression_reason=budget_reason,
                            subject_or_title=f"Reminder: {class_name}",
                            metadata_json={"occurrence_id": str(occ.id), "schedule_version": job.schedule_version}
                        ))
                    else:
                        if job.reminder_type == "REMINDER_2H":
                            subj, _ = render_class_reminder_2h_email(
                                student_name=user.full_name,
                                studio_name=studio_name,
                                class_name=class_name,
                                teacher_name=teacher_name,
                                start_time_ist=time_ist,
                                duration_minutes=duration_mins,
                                suggested_join_time_ist=join_early_ist,
                                join_url=join_url
                            )
                        else:
                            subj, _ = render_class_reminder_15m_email(
                                student_name=user.full_name,
                                studio_name=studio_name,
                                class_name=class_name,
                                teacher_name=teacher_name,
                                start_time_ist=time_ist,
                                suggested_join_time_ist=join_early_ist,
                                join_url=join_url
                            )

                        db.add(CommunicationLog(
                            recipient_user_id=user.id,
                            organization_id=occ.organization_id,
                            recipient_email=user.email,
                            channel="EMAIL",
                            message_type=job.reminder_type,
                            status="SENT",
                            subject_or_title=subj,
                            body_preview=f"{class_name} at {studio_name} ({time_ist})",
                            metadata_json={"occurrence_id": str(occ.id), "schedule_version": job.schedule_version}
                        ))
                        job_sent = True
                        total_dispatched += 1


        job.status = "SENT" if job_sent else "SUPPRESSED"
        job.sent_at = now
        job.details_snapshot = {
            "studio_name": studio_name,
            "class_name": class_name,
            "teacher_name": teacher_name,
            "start_time_ist": time_ist
        }

    await db.commit()
    return total_dispatched


async def process_debounced_material_corrections(
    db: AsyncSession,
    as_of_time: Optional[datetime] = None,
    occurrence_id: Optional[uuid.UUID] = None
) -> int:
    """
    Finds and dispatches consolidated correction notifications for occurrences whose
    debounce window has expired (debounce_until <= as_of_time).
    Merges repeated changes into a single consolidated correction notification.
    """
    now = as_of_time or datetime.now(timezone.utc)
    settings_summary = await get_or_create_settings(db)

    stmt = (
        select(PendingMaterialChange)
        .options(
            selectinload(PendingMaterialChange.occurrence)
            .selectinload(ClassOccurrence.session)
            .selectinload(Session.organization),
            selectinload(PendingMaterialChange.occurrence)
            .selectinload(ClassOccurrence.instructor)
            .selectinload(OrganizationMember.user),
            selectinload(PendingMaterialChange.occurrence)
            .selectinload(ClassOccurrence.session)
            .selectinload(Session.enrollments)
            .selectinload(SessionEnrollment.user),
        )
        .where(
            PendingMaterialChange.processed == False,
            PendingMaterialChange.debounce_until <= now
        )
    )
    if occurrence_id:
        stmt = stmt.where(PendingMaterialChange.class_occurrence_id == occurrence_id)

    result = await db.execute(stmt)
    due_changes = result.scalars().all()

    total_corrections_sent = 0

    for change in due_changes:
        occ = change.occurrence
        if not occ or not occ.session or occ.status == "CANCELLED":
            change.processed = True
            continue

        from app.modules.billing.saas import operational_access
        if not await operational_access(db, occ.organization_id):
            continue

        studio_name = occ.session.organization.name if occ.session.organization else "Studio"
        class_name = occ.session.name
        latest_time_ist = format_ist_time(occ.actual_start_at)
        join_url = f"{getattr(settings, 'WEBSITE_PUBLIC_ORIGIN', 'http://localhost:3000')}/student?studio={occ.organization_id}&class={occ.session_id}&occ={occ.id}"

        # Construct readable change summary
        diff_lines = []
        for field, new_val in change.changes_summary.items():
            if field == "actual_start_at":
                diff_lines.append(f"• Start Time updated to: {latest_time_ist}")
            elif field == "instructor_id":
                teacher_name = occ.instructor.user.full_name if (occ.instructor and occ.instructor.user) else "New Instructor"
                diff_lines.append(f"• Instructor updated to: {teacher_name}")
            else:
                diff_lines.append(f"• {field.replace('_', ' ').title()} updated")
        summary_text = "\n".join(diff_lines)

        recipients: List[User] = []
        if occ.session.enrollments:
            for enr in occ.session.enrollments:
                if enr.status == "ACTIVE" and enr.user and enr.user not in recipients:
                    recipients.append(enr.user)

        for user in recipients:
            pref = await db.scalar(
                select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == user.id)
            )

            # WhatsApp correction
            wa_target = user.whatsapp_number or user.phone
            if wa_target:
                if not settings_summary.whatsapp_enabled:
                    db.add(CommunicationLog(
                        recipient_user_id=user.id,
                        organization_id=occ.organization_id,
                        recipient_phone=wa_target,
                        channel="WHATSAPP_CLASSES",
                        message_type="CLASS_CORRECTION",
                        status="SUPPRESSED",
                        suppression_reason="Suppressed: whatsapp disabled",
                        subject_or_title=f"⚠️ Update: {class_name}"
                    ))
                elif pref and pref.whatsapp_opted_out:
                    db.add(CommunicationLog(
                        recipient_user_id=user.id,
                        organization_id=occ.organization_id,
                        recipient_phone=wa_target,
                        channel="WHATSAPP_CLASSES",
                        message_type="CLASS_CORRECTION",
                        status="SUPPRESSED",
                        suppression_reason="Suppressed: recipient opt-out",
                        subject_or_title=f"⚠️ Update: {class_name}"
                    ))
                else:
                    budget_ok, budget_reason = await check_and_increment_studio_budget(
                        db=db,
                        organization_id=occ.organization_id,
                        channel="WHATSAPP_CLASSES",
                        message_type="CLASS_CORRECTION"
                    )
                    if not budget_ok:
                        db.add(CommunicationLog(
                            recipient_user_id=user.id,
                            organization_id=occ.organization_id,
                            recipient_phone=wa_target,
                            channel="WHATSAPP_CLASSES",
                            message_type="CLASS_CORRECTION",
                            status="SUPPRESSED",
                            suppression_reason=budget_reason,
                            subject_or_title=f"⚠️ Update: {class_name}",
                            metadata_json={"occurrence_id": str(occ.id), "changes": change.changes_summary}
                        ))
                    else:
                        wa_body = render_class_correction_wa(
                            studio_name=studio_name,
                            class_name=class_name,
                            changes_summary=summary_text,
                            latest_start_time_ist=latest_time_ist,
                            join_url=join_url
                        )
                        db.add(CommunicationLog(
                            recipient_user_id=user.id,
                            organization_id=occ.organization_id,
                            recipient_phone=wa_target,
                            channel="WHATSAPP_CLASSES",
                            message_type="CLASS_CORRECTION",
                            status="SENT",
                            subject_or_title=f"⚠️ Update: {class_name}",
                            body_preview=wa_body[:200],
                            metadata_json={"occurrence_id": str(occ.id), "changes": change.changes_summary}
                        ))
                        total_corrections_sent += 1

            # Email correction
            if user.email:
                if not settings_summary.email_enabled:
                    db.add(CommunicationLog(
                        recipient_user_id=user.id,
                        organization_id=occ.organization_id,
                        recipient_email=user.email,
                        channel="EMAIL",
                        message_type="CLASS_CORRECTION",
                        status="SUPPRESSED",
                        suppression_reason="Suppressed: email disabled",
                        subject_or_title=f"⚠️ Update: {class_name}"
                    ))
                elif pref and pref.email_opted_out:
                    db.add(CommunicationLog(
                        recipient_user_id=user.id,
                        organization_id=occ.organization_id,
                        recipient_email=user.email,
                        channel="EMAIL",
                        message_type="CLASS_CORRECTION",
                        status="SUPPRESSED",
                        suppression_reason="Suppressed: recipient opt-out",
                        subject_or_title=f"⚠️ Update: {class_name}"
                    ))
                else:
                    budget_ok, budget_reason = await check_and_increment_studio_budget(
                        db=db,
                        organization_id=occ.organization_id,
                        channel="EMAIL",
                        message_type="CLASS_CORRECTION"
                    )
                    if not budget_ok:
                        db.add(CommunicationLog(
                            recipient_user_id=user.id,
                            organization_id=occ.organization_id,
                            recipient_email=user.email,
                            channel="EMAIL",
                            message_type="CLASS_CORRECTION",
                            status="SUPPRESSED",
                            suppression_reason=budget_reason,
                            subject_or_title=f"⚠️ Update: {class_name}",
                            metadata_json={"occurrence_id": str(occ.id), "changes": change.changes_summary}
                        ))
                    else:
                        subj, _ = render_class_correction_email(
                            student_name=user.full_name,
                            studio_name=studio_name,
                            class_name=class_name,
                            changes_summary=summary_text,
                            latest_start_time_ist=latest_time_ist,
                            join_url=join_url
                        )
                        db.add(CommunicationLog(
                            recipient_user_id=user.id,
                            organization_id=occ.organization_id,
                            recipient_email=user.email,
                            channel="EMAIL",
                            message_type="CLASS_CORRECTION",
                            status="SENT",
                            subject_or_title=subj,
                            body_preview=summary_text[:200],
                            metadata_json={"occurrence_id": str(occ.id), "changes": change.changes_summary}
                        ))
                        total_corrections_sent += 1


        change.processed = True

    await db.commit()
    return total_corrections_sent
