import uuid
from datetime import datetime, time, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.session import Session
from app.models.schedule import ClassOccurrence
from app.common.dates import ensure_utc
from app.common.enums import OccurrenceStatus

from zoneinfo import ZoneInfo
from app.models.organization import Organization

class SchedulingEngine:
    @staticmethod
    async def generate_occurrences_for_session(db: AsyncSession, session_id: uuid.UUID, days_ahead: int = 30) -> int:
        """Generates concrete ClassOccurrences for the session across rolling days_ahead window in studio timezone."""
        # Flush replacements before refreshing already-loaded rule collections.
        await db.flush()
        stmt = select(Session).options(
            selectinload(Session.schedule_rules),
            selectinload(Session.instructors),
            joinedload(Session.organization)
        ).where(Session.id == session_id).with_for_update().execution_options(populate_existing=True)
        res = await db.execute(stmt)
        session = res.scalar_one_or_none()

        if not session or not session.schedule_rules:
            return 0

        # Find primary instructor
        primary_inst = next((i for i in session.instructors if i.role == "PRIMARY"), None)
        instructor_id = primary_inst.member_id if primary_inst else (session.instructors[0].member_id if session.instructors else None)

        studio_tz_name = (session.organization.timezone if session.organization and session.organization.timezone else None) or "UTC"

        # Check existing starts across horizon window in UTC
        now_utc = datetime.now(timezone.utc)
        horizon_utc_end = now_utc + timedelta(days=days_ahead + 2)
        existing_starts = set(ensure_utc(value) for value in (await db.scalars(
            select(ClassOccurrence.scheduled_start_at).where(
                ClassOccurrence.session_id == session_id,
                ClassOccurrence.scheduled_start_at >= now_utc - timedelta(days=1),
                ClassOccurrence.scheduled_start_at < horizon_utc_end,
            )
        )).all())
        created_count = 0

        for rule in session.schedule_rules:
            if not rule.is_active:
                continue

            rule_tz_name = rule.timezone or studio_tz_name
            try:
                rule_tz = ZoneInfo(rule_tz_name)
            except Exception:
                rule_tz = ZoneInfo("UTC")

            today_local = datetime.now(timezone.utc).date()
            end_horizon_local = today_local + timedelta(days=days_ahead)

            current_date = max(today_local, rule.effective_start_date)
            rule_end = rule.effective_end_date or end_horizon_local
            horizon = min(end_horizon_local, rule_end)

            while current_date <= horizon:
                # Python weekday(): 0=Mon, 6=Sun (matches rule.day_of_week)
                if current_date.weekday() == rule.day_of_week:
                    local_start = datetime.combine(current_date, rule.start_time, tzinfo=rule_tz)
                    if rule.end_time < rule.start_time:
                        # Crosses midnight into the next day
                        local_end = datetime.combine(current_date + timedelta(days=1), rule.end_time, tzinfo=rule_tz)
                    else:
                        local_end = datetime.combine(current_date, rule.end_time, tzinfo=rule_tz)

                    start_dt = local_start.astimezone(timezone.utc)
                    end_dt = local_end.astimezone(timezone.utc)

                    if start_dt not in existing_starts:
                        existing_starts.add(start_dt)
                        occ = ClassOccurrence(
                            session_id=session.id,
                            organization_id=session.organization_id,
                            instructor_id=instructor_id,
                            scheduled_start_at=start_dt,
                            scheduled_end_at=end_dt,
                            actual_start_at=start_dt,
                            actual_end_at=end_dt,
                            status=OccurrenceStatus.SCHEDULED.value,
                            meeting_url=session.meeting_url
                        )
                        db.add(occ)
                        await db.flush()
                        from app.modules.communication.reminder_engine import sync_reminder_schedules_for_occurrence
                        await sync_reminder_schedules_for_occurrence(
                            db=db,
                            occurrence_id=occ.id,
                            new_start_at=start_dt
                        )
                        created_count += 1

                current_date += timedelta(days=1)

        await db.commit()
        return created_count

    @staticmethod
    async def check_instructor_conflict(
        db: AsyncSession,
        instructor_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        exclude_occurrence_id: uuid.UUID | None = None
    ) -> bool:
        """Returns True if instructor has a conflicting scheduled occurrence."""
        stmt = select(ClassOccurrence.id).where(
            ClassOccurrence.instructor_id == instructor_id,
            ClassOccurrence.status.in_([OccurrenceStatus.SCHEDULED.value, OccurrenceStatus.IN_PROGRESS.value]),
            ClassOccurrence.actual_start_at < end_at,
            ClassOccurrence.actual_end_at > start_at
        )
        if exclude_occurrence_id:
            stmt = stmt.where(ClassOccurrence.id != exclude_occurrence_id)

        return await db.scalar(stmt.limit(1)) is not None
