import csv
import io
import math
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import UserRole
from app.core.database import get_db
from app.models.commerce import SessionEnrollment
from app.models.attendance import AttendanceRecord
from app.models.organization import Organization, OrganizationMember
from app.models.session import Session, SessionInstructor, SessionPricing
from app.models.user import User
from app.modules.organizations.dependencies import require_org_role

router = APIRouter(prefix="/organizations/{org_id}/billing", tags=["Studio Billing"])
OwnerContext = tuple[Organization, OrganizationMember | None]


@router.get("/progressrooms")
async def progressrooms_billing(
    org_id: uuid.UUID,
    month: date | None = None,
    teacher_id: uuid.UUID | None = None,
    tenant: OwnerContext = Depends(require_org_role([UserRole.STUDIO])),
    db: AsyncSession = Depends(get_db),
):
    from .platform import dashboard
    result = await dashboard(db, tenant[0], month, teacher_id)
    await db.commit()
    return result


def _bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    if end_date < start_date:
        raise HTTPException(422, "End date must be on or after start date")
    if (end_date - start_date).days > 3660:
        raise HTTPException(422, "Choose a billing range of 10 years or less")
    return (
        datetime.combine(start_date, time.min, tzinfo=timezone.utc),
        datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc),
    )


async def _billing_rows(
    db: AsyncSession,
    org_id: uuid.UUID,
    start_date: date,
    end_date: date,
    teacher_id: uuid.UUID | None,
):
    start_at, end_at = _bounds(start_date, end_date)
    teacher_names = (
        select(SessionInstructor.session_id, User.full_name)
        .join(OrganizationMember, OrganizationMember.id == SessionInstructor.member_id)
        .join(User, User.id == OrganizationMember.user_id)
        .where(OrganizationMember.organization_id == org_id)
    )
    teacher_map: dict[uuid.UUID, list[str]] = defaultdict(list)
    for session_id, name in (await db.execute(teacher_names)).all():
        teacher_map[session_id].append(name)

    stmt = (
        select(SessionEnrollment, Session.name, SessionPricing, User.full_name, User.email)
        .join(Session, Session.id == SessionEnrollment.session_id)
        .join(SessionPricing, SessionPricing.session_id == Session.id)
        .join(User, User.id == SessionEnrollment.user_id)
        .where(
            SessionEnrollment.organization_id == org_id,
            SessionEnrollment.valid_from >= start_at,
            SessionEnrollment.valid_from < end_at,
        )
        .order_by(SessionEnrollment.valid_from.desc(), SessionEnrollment.id)
    )
    if teacher_id:
        stmt = stmt.where(exists(select(SessionInstructor.id).where(
            SessionInstructor.session_id == SessionEnrollment.session_id,
            SessionInstructor.member_id == teacher_id,
        )))
    results = (await db.execute(stmt)).all()
    enrollment_ids = [enrollment.id for enrollment, *_ in results]
    excused_cancelled: dict[uuid.UUID, int] = {}
    if enrollment_ids:
        count_rows = (await db.execute(
            select(AttendanceRecord.session_enrollment_id, func.count(AttendanceRecord.id))
            .where(
                AttendanceRecord.session_enrollment_id.in_(enrollment_ids),
                AttendanceRecord.status.in_(["EXCUSED", "CANCELLED"]),
            )
            .group_by(AttendanceRecord.session_enrollment_id)
        )).all()
        excused_cancelled = dict(count_rows)
    return [
        {
            "id": str(enrollment.id),
            "billing_date": enrollment.valid_from.date().isoformat(),
            "student_name": student_name,
            "student_email": student_email,
            "class_name": session_name,
            "teacher_names": teacher_map.get(enrollment.session_id, []),
            "subscription_type": enrollment.billing_type or pricing.pricing_type,
            "subscription_value": float(enrollment.billing_value if enrollment.billing_value is not None else pricing.price),
            "currency": enrollment.billing_currency or pricing.currency,
            "enrollment_status": enrollment.status,
            "sessions_taken": enrollment.sessions_attended,
            "sessions_excused_or_cancelled": excused_cancelled.get(enrollment.id, 0),
            "source": "SUBSCRIPTION_VALUE",
        }
        for enrollment, session_name, pricing, student_name, student_email in results
    ]


def _bucket(day: date, interval: str) -> tuple[str, str]:
    if interval == "daily":
        return day.isoformat(), day.strftime("%d %b")
    if interval == "weekly":
        monday = day - timedelta(days=day.weekday())
        return monday.isoformat(), monday.strftime("%d %b")
    month = day.replace(day=1)
    return month.isoformat(), month.strftime("%b %Y")


@router.get("/teachers")
async def billing_teachers(
    org_id: uuid.UUID,
    tenant: OwnerContext = Depends(require_org_role([UserRole.STUDIO])),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(OrganizationMember.id, User.full_name)
        .join(User, User.id == OrganizationMember.user_id)
        .where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.is_active.is_(True),
            OrganizationMember.role.in_(["STUDIO", "TEACHER"]),
        )
        .order_by(User.full_name)
    )).all()
    return [{"id": str(member_id), "name": name} for member_id, name in rows]


@router.get("/summary")
async def billing_summary(
    org_id: uuid.UUID,
    start_date: date,
    end_date: date,
    interval: Literal["daily", "weekly", "monthly"] = "monthly",
    teacher_id: uuid.UUID | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    sort_by: Literal["date", "name", "amount", "sessions"] = "date",
    sort_order: Literal["asc", "desc"] = "desc",
    tenant: OwnerContext = Depends(require_org_role([UserRole.STUDIO])),
    db: AsyncSession = Depends(get_db),
):
    rows = await _billing_rows(db, org_id, start_date, end_date, teacher_id)
    totals: dict[str, Decimal] = defaultdict(Decimal)
    labels: dict[str, str] = {}
    for row in rows:
        key, label = _bucket(date.fromisoformat(row["billing_date"]), interval)
        totals[key] += Decimal(str(row["subscription_value"]))
        labels[key] = label
    currency = rows[0]["currency"] if rows else tenant[0].currency
    sort_key = {
        "date": lambda row: row["billing_date"],
        "name": lambda row: row["student_name"].casefold(),
        "amount": lambda row: row["subscription_value"],
        "sessions": lambda row: row["sessions_taken"],
    }[sort_by]
    sorted_rows = sorted(rows, key=sort_key, reverse=sort_order == "desc")
    paged_rows = sorted_rows[(page - 1) * page_size:page * page_size]
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "interval": interval,
        "currency": currency,
        "total_billed": float(sum(totals.values(), Decimal())),
        "billing_count": len(rows),
        "average_billing": round(sum((Decimal(str(r["subscription_value"])) for r in rows), Decimal()) / len(rows), 2) if rows else 0,
        "active_count": sum(row["enrollment_status"] == "ACTIVE" for row in rows),
        "series": [{"period": key, "label": labels[key], "amount": float(totals[key])} for key in sorted(totals)],
        "rows": paged_rows,
        "total_items": len(rows),
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, math.ceil(len(rows) / page_size)),
        "sort_by": sort_by,
        "sort_order": sort_order,
        "calculation_note": "Projected billing from enrollment subscription values; payment transactions are excluded.",
    }


@router.get("/export.csv")
async def export_billing_csv(
    org_id: uuid.UUID,
    start_date: date,
    end_date: date,
    teacher_id: uuid.UUID | None = None,
    tenant: OwnerContext = Depends(require_org_role([UserRole.STUDIO])),
    db: AsyncSession = Depends(get_db),
):
    rows = await _billing_rows(db, org_id, start_date, end_date, teacher_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Billing date", "Student", "Email", "Class", "Teacher(s)", "Subscription type", "Subscription value", "Currency", "Sessions taken", "Sessions excused/cancelled", "Enrollment status"])
    for row in rows:
        writer.writerow([
            row["billing_date"], row["student_name"], row["student_email"], row["class_name"],
            ", ".join(row["teacher_names"]), row["subscription_type"],
            f'{row["subscription_value"]:.2f}', row["currency"], row["sessions_taken"],
            row["sessions_excused_or_cancelled"], row["enrollment_status"],
        ])
    filename = f"billing-{start_date.isoformat()}-to-{end_date.isoformat()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
