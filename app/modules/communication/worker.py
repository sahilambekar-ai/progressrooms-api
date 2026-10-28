"""Run with python -m app.modules.communication.worker [--once]. No public endpoint."""
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import select, update
from app.common.dates import ensure_utc
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.communication import MeetingEmailDelivery, CommunicationLog, UserCommunicationPreference
from app.models.schedule import ClassOccurrence
from app.models.session import Session
from app.models.user import User
from app.modules.communication.service import get_or_create_settings
from app.modules.communication.reminder_engine import (backfill_reminders, dispatch_pending_reminders,
    process_debounced_material_corrections, recipients_for, render_meeting_email)
from app.modules.communication.zoom_mail import send_zoom_email
from app.modules.zoom.meetings import host_for_occurrence
from app.modules.zoom.provider import ProviderError
from app.modules.zoom.service import access_token

logger = logging.getLogger(__name__)


async def deliver_one(db, delivery_id):
    now = datetime.now(timezone.utc)
    # Atomic claim prevents two worker instances from submitting the same email.
    claimed = await db.execute(update(MeetingEmailDelivery).where(MeetingEmailDelivery.id == delivery_id,
        MeetingEmailDelivery.status.in_(["PENDING", "RETRY"]), MeetingEmailDelivery.available_at <= now)
        .values(status="SENDING", attempts=MeetingEmailDelivery.attempts + 1, updated_at=now).execution_options(synchronize_session=False))
    await db.commit()
    if not claimed.rowcount:
        return
    row = await db.get(MeetingEmailDelivery, delivery_id, populate_existing=True)
    occ = await db.scalar(select(ClassOccurrence).where(ClassOccurrence.id == row.class_occurrence_id).with_for_update())
    user = await db.get(User, row.recipient_user_id)
    session = await db.get(Session, occ.session_id) if occ else None
    pref = await db.scalar(select(UserCommunicationPreference).where(UserCommunicationPreference.user_id == row.recipient_user_id))
    channels = await get_or_create_settings(db)
    subject, body = "Meeting notification", ""
    failure = None
    provider_id = None
    connection = None
    try:
        if not user or not user.is_active or not occ or not session:
            row.status, failure = "SUPPRESSED", "Recipient or class is unavailable"
        elif not channels.email_enabled:
            row.status, failure = "SUPPRESSED", "Operational email is disabled at the platform level"
        elif pref and pref.email_opted_out:
            row.status, failure = "SUPPRESSED", "Recipient has opted out of email notifications"
        elif row.message_type != "CLASS_CANCELLATION" and (occ.status == "CANCELLED" or (not session.is_active and session.session_type != "ONE_TO_ONE" and row.message_type != "CLASS_CORRECTION") or session.deleted_at):
            row.status, failure = "CANCELLED", "Class is cancelled or inactive"
        elif row.message_type in {"REMINDER_30M", "CLASS_INVITE"} and ensure_utc(occ.actual_start_at) != ensure_utc(row.scheduled_start_at):
            row.status, failure = "SUPERSEDED", "Class was rescheduled"
        elif row.message_type != "CLASS_CANCELLATION" and ensure_utc(occ.actual_end_at) <= now:
            row.status, failure = "EXPIRED", "Class has ended"
        elif row.message_type == "REMINDER_30M" and ensure_utc(occ.actual_start_at) <= now:
            row.status, failure = "EXPIRED", "Class has already started"
        elif row.message_type != "CLASS_CANCELLATION" and user.id not in {u.id for u in await recipients_for(db, occ)}:
            row.status, failure = "SUPPRESSED", "Enrollment or teacher assignment is no longer eligible"
        else:
            from app.modules.billing.saas import operational_access
            if row.message_type != "CLASS_CANCELLATION" and not await operational_access(db, occ.organization_id):
                row.status, failure = "SUPPRESSED", "Studio operational access is paused"
            else:
                connection = await host_for_occurrence(db, occ)
                # Serialize token refresh and sending with other operations on this host.
                if connection.mode != "mock":
                    await access_token(db, connection)
                subject, body = await render_meeting_email(db, occ, user, row.message_type, row.reason)
                result = await send_zoom_email(db, connection, user.email, subject, body, row.id)
                provider_id = result["id"]
                row.status = "SIMULATED" if result.get("simulated") else "SENT"
    except ProviderError as error:
        failure = str(error)
        if error.invalid_grant and connection is not None:
            connection.status, connection.tokens_enc = "RECONNECT_REQUIRED", None
        if error.ambiguous:
            row.status = "UNCERTAIN"
        elif getattr(error, "status_code", None) == 429 and row.attempts < 5:
            row.status = "RETRY"
            row.available_at = now + timedelta(seconds=max(60, getattr(error, "retry_after", 0) or 0, 30 * 2 ** row.attempts))
        else:
            row.status = "FAILED"
    except HTTPException as error:
        row.status, failure = "FAILED", str(error.detail)
    except Exception as error:
        # Submission may have reached Zoom. Do not repeat an unknown send automatically.
        row.status, failure = "UNCERTAIN", "Delivery outcome needs review; no automatic resend"
        logger.error("Meeting mail delivery needs review: %s error_type=%s", delivery_id, type(error).__name__)
    row.provider_message_id = provider_id
    db.add(CommunicationLog(recipient_user_id=row.recipient_user_id, organization_id=occ.organization_id if occ else None,
        recipient_email=user.email if user else None, channel="ZOOM_MAIL", message_type=row.message_type,
        status=row.status, subject_or_title=subject, body_preview=body[:200],
        error_details=failure, suppression_reason=failure if row.status in {"SUPPRESSED", "CANCELLED", "SUPERSEDED", "EXPIRED"} else None,
        metadata_json={"delivery_id": str(row.id), "occurrence_id": str(row.class_occurrence_id), "provider_message_id": provider_id}))
    await db.commit()


async def run_cycle(session_factory=AsyncSessionLocal):
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        await get_or_create_settings(db)
        org_id = settings.MEETING_EMAIL_ORGANIZATION_ID
        await backfill_reminders(db, now, organization_id=org_id)
        await dispatch_pending_reminders(db, now, organization_id=org_id)
        await process_debounced_material_corrections(db, now, organization_id=org_id)
        # A crash after the claim must not cause an automatic duplicate send.
        await db.execute(update(MeetingEmailDelivery).where(MeetingEmailDelivery.status == "SENDING",
            MeetingEmailDelivery.updated_at < now - timedelta(minutes=10),
            *([MeetingEmailDelivery.class_occurrence_id.in_(select(ClassOccurrence.id).where(ClassOccurrence.organization_id == org_id))] if org_id else [])).values(status="UNCERTAIN",
            reason="Worker stopped during submission; verify Zoom sent mail before retrying"))
        await db.commit()
        ids = list((await db.scalars(select(MeetingEmailDelivery.id).where(
            MeetingEmailDelivery.status.in_(["PENDING", "RETRY"]), MeetingEmailDelivery.available_at <= now,
            *([MeetingEmailDelivery.class_occurrence_id.in_(select(ClassOccurrence.id).where(ClassOccurrence.organization_id == org_id))] if org_id else []))
            .order_by(MeetingEmailDelivery.available_at).limit(max(1, min(settings.MEETING_EMAIL_BATCH_SIZE, 500))))).all())
    semaphore = asyncio.Semaphore(max(1, min(settings.MEETING_EMAIL_CONCURRENCY, 10)))
    async def dispatch(delivery_id):
        async with semaphore:
            async with session_factory() as db:
                try:
                    await deliver_one(db, delivery_id)
                except Exception as error:
                    await db.rollback()
                    logger.error("Meeting mail worker failed for %s error_type=%s", delivery_id, type(error).__name__)
    await asyncio.gather(*(dispatch(delivery_id) for delivery_id in ids))
    return len(ids)


async def _run():
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            await run_cycle()
        except Exception as error:
            logger.error("Meeting email scheduler cycle failed error_type=%s", type(error).__name__)
        if "--once" in sys.argv:
            return
        await asyncio.sleep(max(5, settings.MEETING_EMAIL_WORKER_INTERVAL_SECONDS))


async def main():
    from app.core.database import engine
    try:
        await _run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
