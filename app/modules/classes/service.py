import uuid
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import Session, SessionPricing
from app.models.schedule import ClassOccurrence
from app.models.attendance import AttendanceRecord
from app.common.enums import SessionType, OccurrenceStatus, AttendanceStatus
from app.common.dates import ensure_utc

async def auto_complete_expired_one_on_one_classes(db: AsyncSession, org_id: uuid.UUID) -> int:
    """
    Checks for any active ONE_TO_ONE sessions whose occurrence end time has elapsed.
    - Marks the occurrence COMPLETED
    - Automatically logs ATTENDED attendance for student enrollments
    - Deletes/soft-deletes the session so it is removed from active class batches
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(Session)
        .options(
            selectinload(Session.occurrences).selectinload(ClassOccurrence.attendance_records),
            selectinload(Session.enrollments)
        )
        .where(
            Session.organization_id == org_id,
            Session.session_type == SessionType.ONE_TO_ONE.value,
            Session.deleted_at.is_(None),
            Session.pricing.has((SessionPricing.pricing_type == "PER_SESSION") & (SessionPricing.total_classes == 1)),
            Session.occurrences.any(ClassOccurrence.actual_end_at <= now)
        )
    )
    res = await db.execute(stmt)
    sessions = res.scalars().all()
    if not sessions:
        return 0

    completed_count = 0
    for sess in sessions:
        all_completed = True
        for occ in sess.occurrences:
            occ_end = ensure_utc(occ.actual_end_at)
            if occ_end <= now:
                if occ.status not in (OccurrenceStatus.COMPLETED.value, OccurrenceStatus.CANCELLED.value):
                    occ.status = OccurrenceStatus.COMPLETED.value
                    completed_count += 1

                if occ.status == OccurrenceStatus.CANCELLED.value:
                    continue

                # Record attendance for all enrolled students
                for enr in sess.enrollments:
                    existing_att = next((a for a in occ.attendance_records if a.session_enrollment_id == enr.id), None)
                    if not existing_att:
                        att = AttendanceRecord(
                            class_occurrence_id=occ.id,
                            session_enrollment_id=enr.id,
                            status=AttendanceStatus.ATTENDED.value,
                            marked_at=occ.actual_end_at,
                            notes="Automatically recorded upon 1-on-1 session completion"
                        )
                        db.add(att)
                        enr.sessions_attended = max(enr.sessions_attended, 1)
                        completed_count += 1
            else:
                all_completed = False

        if sess.occurrences and all_completed:
            sess.deleted_at = now
            sess.is_active = False
            completed_count += 1

    if completed_count > 0:
        await db.commit()

    return completed_count
