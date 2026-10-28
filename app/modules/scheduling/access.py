from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from app.models.session import SessionInstructor, Session
from app.models.schedule import ClassOccurrence


def assigned_occurrence(member_id):
    # An explicit replacement teacher overrides the recurring class assignment.
    return or_(ClassOccurrence.instructor_id == member_id,
               and_(ClassOccurrence.instructor_id.is_(None), ClassOccurrence.session_id.in_(
                   select(SessionInstructor.session_id).where(SessionInstructor.member_id == member_id))))

async def accessible_occurrence(db, org_id, occurrence_id, member, lock=False):
    stmt = select(ClassOccurrence).join(Session, Session.id == ClassOccurrence.session_id).where(
        (Session.deleted_at.is_(None) | (ClassOccurrence.status == "COMPLETED")),ClassOccurrence.id == occurrence_id, ClassOccurrence.organization_id == org_id)
    if member and member.role == "TEACHER":
        stmt = stmt.where(assigned_occurrence(member.id))
    if lock:
        stmt = stmt.with_for_update()
    occ = await db.scalar(stmt)
    if not occ:
        raise HTTPException(404, "Class not found or not assigned to you")
    return occ
