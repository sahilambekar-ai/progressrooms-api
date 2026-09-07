import uuid
from datetime import datetime, date, timedelta, timezone
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.session import Session, SessionInstructor
from app.models.schedule import SessionScheduleRule, ClassOccurrence, ClassOccurrenceChange
from app.common.enums import OccurrenceStatus

class SchedulingEngine:
    @staticmethod
    async def generate_occurrences_for_session(db: AsyncSession, session_id: uuid.UUID, days_ahead: int = 30) -> int:
        """Generates concrete ClassOccurrences for the session across rolling days_ahead window."""
        stmt = select(Session).options(
            selectinload(Session.schedule_rules),
            selectinload(Session.instructors)
        ).where(Session.id == session_id)
        res = await db.execute(stmt)
        session = res.scalar_one_or_none()

        if not session or not session.schedule_rules:
            return 0

        # Find primary instructor
        primary_inst = next((i for i in session.instructors if i.role == "PRIMARY"), None)
        instructor_id = primary_inst.member_id if primary_inst else (session.instructors[0].member_id if session.instructors else None)

        today = datetime.now(timezone.utc).date()
        end_horizon = today + timedelta(days=days_ahead)

        created_count = 0

        for rule in session.schedule_rules:
            if not rule.is_active:
                continue

            current_date = max(today, rule.effective_start_date)
            rule_end = rule.effective_end_date or end_horizon
            horizon = min(end_horizon, rule_end)

            while current_date <= horizon:
                # Python weekday(): 0=Mon, 6=Sun (matches rule.day_of_week)
                if current_date.weekday() == rule.day_of_week:
                    start_dt = datetime.combine(current_date, rule.start_time, tzinfo=timezone.utc)
                    end_dt = datetime.combine(current_date, rule.end_time, tzinfo=timezone.utc)

                    # Duplicate check
                    dup_stmt = select(ClassOccurrence).where(
                        ClassOccurrence.session_id == session_id,
                        ClassOccurrence.scheduled_start_at == start_dt
                    )
                    dup_res = await db.execute(dup_stmt)
                    if not dup_res.scalar_one_or_none():
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
        stmt = select(ClassOccurrence).where(
            ClassOccurrence.instructor_id == instructor_id,
            ClassOccurrence.status.in_([OccurrenceStatus.SCHEDULED.value, OccurrenceStatus.IN_PROGRESS.value]),
            ClassOccurrence.actual_start_at < end_at,
            ClassOccurrence.actual_end_at > start_at
        )
        if exclude_occurrence_id:
            stmt = stmt.where(ClassOccurrence.id != exclude_occurrence_id)

        res = await db.execute(stmt)
        return res.scalar_one_or_none() is not None
