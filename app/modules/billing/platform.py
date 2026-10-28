"""Platform fees use unique qualified students, never attendance or enrollment totals."""
import uuid
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select
from app.common.dates import ensure_utc
from app.models.commerce import Order, SessionEnrollment
from app.models.organization import Organization, OrganizationMember, OrganizationStudent, OrganizationSetting
from app.models.user import User
from app.models.platform_billing import PlatformInvoice, StudentJoinEvent, StudentJoinTeacher
from app.models.session import SessionInstructor

FREE_LIMIT = 20
RATE = 30


def fee(count):
    return count * RATE if count > FREE_LIMIT else 0


def month_at(org, now):
    return ensure_utc(now).astimezone(ZoneInfo(org.timezone)).date().replace(day=1)


def month_bounds(org, month):
    following = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    zone = ZoneInfo(org.timezone)
    return (datetime.combine(month, time.min, zone).astimezone(timezone.utc),
            datetime.combine(following, time.min, zone).astimezone(timezone.utc))


async def finalize_months(db, org, now):
    """Caller holds the organization lock; invoice amounts are never recalculated."""
    current = month_at(org, now)
    existing = select(PlatformInvoice.month).where(PlatformInvoice.organization_id == org.id)
    rows = (await db.execute(select(StudentJoinEvent.month, func.count(func.distinct(StudentJoinEvent.user_id)))
        .where(StudentJoinEvent.organization_id == org.id, StudentJoinEvent.month < current,
               StudentJoinEvent.month.not_in(existing)).group_by(StudentJoinEvent.month))).all()
    generated = 0
    for month, count in rows:
        unresolved = await db.scalar(select(StudentJoinEvent.id).where(StudentJoinEvent.organization_id == org.id,
            StudentJoinEvent.month == month,
            or_(StudentJoinEvent.pricing_region.is_(None), StudentJoinEvent.pricing_region != "INDIA")).limit(1))
        if unresolved:
            continue  # International/unknown country usage needs a support quote.
        db.add(PlatformInvoice(organization_id=org.id, month=month, active_students=count,
            free_limit=FREE_LIMIT, rate=RATE, amount=fee(count), currency="INR", generated_at=now))
        generated += 1
    await db.flush()
    return generated


async def record_join(db, org, user, enrollment, occurrence):
    # Serialize month rollover with joins. Capture time AFTER the lock so a waiting
    # request cannot append to a month whose invoice has already been frozen.
    await db.execute(select(Organization.id).where(Organization.id == org.id).with_for_update())
    enrollment = await db.scalar(select(SessionEnrollment).where(SessionEnrollment.id == enrollment.id)
        .with_for_update().execution_options(populate_existing=True))
    now = datetime.now(timezone.utc)
    if user.role != "STUDENT":
        return
    if not enrollment or enrollment.organization_id != org.id or enrollment.user_id != user.id or (
        occurrence.organization_id != org.id or enrollment.session_id != occurrence.session_id):
        raise HTTPException(403, "No active subscription for this class")
    if enrollment.status != "ACTIVE" or ensure_utc(enrollment.valid_from) > ensure_utc(occurrence.actual_start_at) or (
        enrollment.valid_until and ensure_utc(enrollment.valid_until) < ensure_utc(occurrence.actual_start_at)):
        raise HTTPException(403, "Your subscription is no longer active")
    from app.modules.commerce.service import available_credit
    if not await available_credit(db, enrollment):
        raise HTTPException(403, "Your pass or course has no sessions remaining")
    await finalize_months(db, org, now)
    event = StudentJoinEvent(organization_id=org.id, user_id=user.id, enrollment_id=enrollment.id,
        occurrence_id=occurrence.id, month=month_at(org, now), clicked_at=now)
    db.add(event)
    await db.flush()
    # Snapshot the actual teacher, independent of subsequent roster changes.
    teacher_ids = [occurrence.instructor_id] if occurrence.instructor_id else (await db.scalars(
        select(SessionInstructor.member_id).join(OrganizationMember, OrganizationMember.id == SessionInstructor.member_id)
        .where(SessionInstructor.session_id == occurrence.session_id, OrganizationMember.organization_id == org.id))).all()
    for teacher_id in set(teacher_ids):
        db.add(StudentJoinTeacher(event_id=event.id, teacher_id=teacher_id))
    studio_settings = await db.scalar(select(OrganizationSetting).where(OrganizationSetting.organization_id == org.id))
    studio_country = (studio_settings.country or "").strip().upper() if studio_settings else ""
    teacher_countries = (await db.scalars(select(User.billing_country).join(OrganizationMember,
        OrganizationMember.user_id == User.id).where(OrganizationMember.id.in_(set(teacher_ids))))).all()
    countries = ["IN" if studio_country in {"IN", "INDIA"} else studio_country, user.billing_country, *teacher_countries]
    event.pricing_region = ("INTERNATIONAL" if any(c and c != "IN" for c in countries) else
                            "INDIA" if teacher_countries and all(c == "IN" for c in countries) else "UNCONFIRMED")
    await db.flush()


def scoped_events(org_id, month, teacher_id=None):
    stmt = select(StudentJoinEvent).where(StudentJoinEvent.organization_id == org_id, StudentJoinEvent.month == month)
    if teacher_id:
        stmt = stmt.where(exists(select(StudentJoinTeacher.event_id).where(
            StudentJoinTeacher.event_id == StudentJoinEvent.id, StudentJoinTeacher.teacher_id == teacher_id)))
    return stmt


async def dashboard(db, org, month=None, teacher_id=None, now=None):
    now = now or datetime.now(timezone.utc)
    current = month_at(org, now)
    month = month or current
    if month.day != 1 or month > current:
        raise HTTPException(422, "Choose the first day of a current or past month")
    if teacher_id and not await db.scalar(select(OrganizationMember.id).where(
        OrganizationMember.id == teacher_id, OrganizationMember.organization_id == org.id,
        OrganizationMember.can_teach.is_(True))):
        raise HTTPException(404, "Teacher not found in this studio")
    await db.execute(select(Organization.id).where(Organization.id == org.id).with_for_update())
    await finalize_months(db, org, now)
    events = scoped_events(org.id, month, teacher_id).subquery()
    active, clicks = (await db.execute(select(func.count(func.distinct(events.c.user_id)), func.count(events.c.id)))).one()
    studio_events = scoped_events(org.id, month).subquery()
    studio_active = await db.scalar(select(func.count(func.distinct(studio_events.c.user_id)))) or 0
    pricing_review = bool(await db.scalar(select(studio_events.c.id).where(or_(
        studio_events.c.pricing_region.is_(None), studio_events.c.pricing_region != "INDIA")).limit(1)))
    start, end = month_bounds(org, month)
    enrollment_stmt = select(SessionEnrollment).where(SessionEnrollment.organization_id == org.id)
    if teacher_id:
        enrollment_stmt = enrollment_stmt.where(exists(select(SessionInstructor.id).where(
            SessionInstructor.session_id == SessionEnrollment.session_id, SessionInstructor.member_id == teacher_id)))
    # Current active plans are subscriptions with valid access and available credits.
    from app.modules.commerce.service import available_credit
    active_plans = []
    for enrollment in (await db.scalars(enrollment_stmt.where(SessionEnrollment.status == "ACTIVE",
        SessionEnrollment.valid_from <= now, or_(SessionEnrollment.valid_until.is_(None), SessionEnrollment.valid_until > now)))).all():
        if await available_credit(db, enrollment, lock_pass=False):
            active_plans.append(enrollment)
    # Collected order revenue is counted once per order and grouped by currency.
    orders = select(Order.currency, func.sum(Order.total_amount)).where(Order.organization_id == org.id, Order.status == "PAID")
    if teacher_id:
        orders = orders.where(exists(select(SessionEnrollment.id).where(
            SessionEnrollment.organization_id == org.id,
            or_(SessionEnrollment.order_id == Order.id, SessionEnrollment.purchase_order_id == Order.id),
            exists(select(SessionInstructor.id).where(SessionInstructor.session_id == SessionEnrollment.session_id,
                SessionInstructor.member_id == teacher_id)))))
    revenue = [{"currency": currency, "amount": float(amount)} for currency, amount in
               (await db.execute(orders.group_by(Order.currency))).all()]
    invoices = (await db.scalars(select(PlatformInvoice).where(PlatformInvoice.organization_id == org.id)
        .order_by(PlatformInvoice.month.desc()))).all()
    invoice = next((i for i in invoices if i.month == month), None)
    total_teachers = await db.scalar(select(func.count(OrganizationMember.id)).where(
        OrganizationMember.organization_id == org.id, OrganizationMember.can_teach.is_(True))) or 0
    total_students = await db.scalar(select(func.count(OrganizationStudent.id)).where(OrganizationStudent.organization_id == org.id)) or 0
    # A multi-class pass creates several enrollments but remains one subscription.
    subscriptions = {}
    for enrollment in active_plans:
        subscriptions.setdefault(enrollment.purchase_order_id or enrollment.order_id or enrollment.id, enrollment)
    plans = {}
    for enrollment in subscriptions.values():
        label = enrollment.billing_label or "Class subscription"
        plans[label] = plans.get(label, 0) + 1
    return dict(organization_id=str(org.id), studio_name=org.name, month=month.isoformat(), timezone=org.timezone,
        period_start=start.isoformat(), period_end=end.isoformat(), teacher_id=str(teacher_id) if teacher_id else None,
        active_students=active, join_clicks=clicks, studio_active_students=studio_active,
        total_teachers=total_teachers, total_students=total_students, active_subscriptions=len(subscriptions),
        active_plans=[dict(name=name, subscriptions=count) for name, count in sorted(plans.items())],
        total_revenue=revenue, free_limit=FREE_LIMIT, rate=RATE, currency="INR",
        platform_amount=float(invoice.amount) if invoice else (None if pricing_review else fee(studio_active)),
        billing_status="GENERATED" if invoice else ("CONTACT_SUPPORT" if pricing_review else ("ESTIMATE" if month == current else "NO_ACTIVITY")),
        invoices=[dict(id=str(i.id), number=f"PR-{i.month:%Y%m}-{str(i.organization_id)[:8].upper()}",
            month=i.month.isoformat(), active_students=i.active_students, free_limit=i.free_limit, rate=float(i.rate),
            amount=float(i.amount), currency=i.currency, generated_at=i.generated_at.isoformat()) for i in invoices],
        calculation_note="For Indian studios, teachers and students, up to 20 unique active students is free. "
            "Above 20, every active student costs ₹30 before applicable tax, in addition to the subscription, including during its trial. "
            "International or unconfirmed billing countries require a support quote. "
            "A valid subscription and a successful join-link request are required. Fees apply to the whole studio.")
