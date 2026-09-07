import asyncio
import uuid
from datetime import datetime, date, time, timedelta, timezone
from sqlalchemy import select
from app.core.database import AsyncSessionLocal, engine, Base
from app.models.user import User, UserAuthMethod
from app.models.organization import Organization, OrganizationMember, OrganizationSetting, OrganizationStudent
from app.models.plan import Plan, PlanFeature, OrganizationSubscription
from app.models.session import ClassType, Session, SessionInstructor, SessionPricing, SessionRequirement
from app.models.schedule import SessionScheduleRule, ClassOccurrence, ClassOccurrenceChange
from app.models.commerce import Order, OrderItem, Payment, SessionEnrollment, OrganizationPaymentAccount
from app.models.attendance import AttendanceRecord
from app.models.website import WebsiteTemplate, OrganizationWebsite
from app.common.enums import UserRole, SessionType, SkillLevel, PricingType, OccurrenceStatus, OrderStatus, PaymentStatus, EnrollmentStatus, AttendanceStatus

async def seed_database():
    print("Seeding ProgressRooms database...")
    async with AsyncSessionLocal() as db:
        # Check if already seeded
        existing_admin = await db.execute(select(User).where(User.email == "admin@test.progressrooms.local"))
        if existing_admin.scalar_one_or_none():
            print("Database already seeded. Skipping.")
            return

        # 1. Super Admin
        admin_user = User(
            email="admin@test.progressrooms.local",
            full_name="Platform Super Admin",
            is_superadmin=True
        )
        db.add(admin_user)
        await db.flush()

        db.add(UserAuthMethod(
            user_id=admin_user.id,
            auth_type="OTP",
            otp_code="123456",
            otp_expires_at=datetime.now(timezone.utc) + timedelta(days=365)
        ))

        # 2. Website Templates
        tmpl_default = WebsiteTemplate(name="Default Classic", code="DEFAULT")
        tmpl_a = WebsiteTemplate(name="Minimalist Elegance", code="TEMPLATE_A")
        tmpl_b = WebsiteTemplate(name="Studio Active", code="TEMPLATE_B")
        tmpl_c = WebsiteTemplate(name="Modern Academy", code="TEMPLATE_C")
        db.add_all([tmpl_default, tmpl_a, tmpl_b, tmpl_c])
        await db.flush()

        # 3. Subscription Plans & Feature Entitlements
        starter_plan = Plan(name="Starter", slug="starter", price=0.00, billing_interval="MONTHLY", description="30-day trial plan")
        core_plan = Plan(name="Core", slug="core", price=1499.00, billing_interval="MONTHLY", description="Basic paid plan for solo teachers")
        studio_plan = Plan(name="Studio", slug="studio", price=3999.00, billing_interval="MONTHLY", description="Advanced plan for studios & academies")
        ai_plan = Plan(name="Progress AI", slug="progress-ai", price=0.00, status="COMING_SOON", is_public=False, description="Coming soon AI features")
        db.add_all([starter_plan, core_plan, studio_plan, ai_plan])
        await db.flush()

        # Features
        features = [
            # Starter
            PlanFeature(plan_id=starter_plan.id, feature_key="max_teachers", feature_value="1"),
            PlanFeature(plan_id=starter_plan.id, feature_key="max_active_sessions", feature_value="3"),
            PlanFeature(plan_id=starter_plan.id, feature_key="max_students", feature_value="50"),
            PlanFeature(plan_id=starter_plan.id, feature_key="advanced_website", feature_value="false"),
            PlanFeature(plan_id=starter_plan.id, feature_key="custom_domain", feature_value="false"),
            # Core
            PlanFeature(plan_id=core_plan.id, feature_key="max_teachers", feature_value="1"),
            PlanFeature(plan_id=core_plan.id, feature_key="max_active_sessions", feature_value="10"),
            PlanFeature(plan_id=core_plan.id, feature_key="max_students", feature_value="250"),
            PlanFeature(plan_id=core_plan.id, feature_key="advanced_website", feature_value="false"),
            PlanFeature(plan_id=core_plan.id, feature_key="custom_domain", feature_value="false"),
            # Studio
            PlanFeature(plan_id=studio_plan.id, feature_key="max_teachers", feature_value="10"),
            PlanFeature(plan_id=studio_plan.id, feature_key="max_active_sessions", feature_value="-1"),
            PlanFeature(plan_id=studio_plan.id, feature_key="max_students", feature_value="-1"),
            PlanFeature(plan_id=studio_plan.id, feature_key="advanced_website", feature_value="true"),
            PlanFeature(plan_id=studio_plan.id, feature_key="custom_domain", feature_value="true"),
        ]
        db.add_all(features)
        await db.flush()

        # 4. Organization A: Sahil Yoga Studio
        org_a = Organization(name="Sahil Yoga Studio", slug="sahil-yoga-studio", timezone="Asia/Kolkata", currency="INR")
        db.add(org_a)
        await db.flush()

        # Owner & Instructors
        sahil_user = User(email="owner@yogastudio.test", full_name="Sahil Sharma")
        ananya_user = User(email="ananya@yogastudio.test", full_name="Ananya Sen")
        rohit_user = User(email="rohit@yogastudio.test", full_name="Rohit Verma")
        db.add_all([sahil_user, ananya_user, rohit_user])
        await db.flush()

        for u in [sahil_user, ananya_user, rohit_user]:
            db.add(UserAuthMethod(user_id=u.id, auth_type="OTP", otp_code="123456", otp_expires_at=datetime.now(timezone.utc) + timedelta(days=365)))

        mem_owner = OrganizationMember(organization_id=org_a.id, user_id=sahil_user.id, role=UserRole.OWNER.value, title="Founder & Lead Guru", bio="Certified Hatha & Vinyasa master with 12+ years experience.")
        mem_ananya = OrganizationMember(organization_id=org_a.id, user_id=ananya_user.id, role=UserRole.INSTRUCTOR.value, title="Aerial Yoga Specialist", bio="Aerial silks and restorative yoga practitioner.")
        mem_rohit = OrganizationMember(organization_id=org_a.id, user_id=rohit_user.id, role=UserRole.INSTRUCTOR.value, title="Pranayama & Meditation Coach", bio="Mindfulness and breathwork guide.")
        db.add_all([mem_owner, mem_ananya, mem_rohit])

        # Trial Subscription for Org A
        now = datetime.now(timezone.utc)
        sub_a = OrganizationSubscription(
            organization_id=org_a.id,
            plan_id=starter_plan.id,
            status="TRIAL",
            starts_at=now,
            trial_starts_at=now,
            trial_ends_at=now + timedelta(days=30),
            payment_provider="NONE"
        )
        db.add(sub_a)

        # Website Org A
        ws_a = OrganizationWebsite(
            organization_id=org_a.id,
            template_id=tmpl_default.id,
            headline="Cultivate Stillness, Strength & Breath",
            subheadline="Authentic daily Hatha flow, Aerial conditioning, and Himalayan breathwork classes in Bangalore & Online.",
            about_text="Sahil Yoga Studio is a holistic wellness sanctuary dedicated to alignment, breath awareness, and graceful mobility. Whether you are unrolling a mat for the first time or soaring in silks, welcome home.",
            primary_color="#4f46e5",
            accent_color="#06b6d4"
        )
        db.add(ws_a)

        # Class Types
        ct_yoga = ClassType(organization_id=org_a.id, name="Hatha Yoga", slug="hatha-yoga", description="Classical postures and deep somatic breathing.")
        ct_aerial = ClassType(organization_id=org_a.id, name="Aerial Yoga", slug="aerial-yoga", description="Decompress the spine with suspended hammocks.")
        ct_med = ClassType(organization_id=org_a.id, name="Meditation & Pranayama", slug="meditation", description="Mindfulness and deep restorative stillness.")
        db.add_all([ct_yoga, ct_aerial, ct_med])
        await db.flush()

        # Sessions for Org A
        s1 = Session(
            organization_id=org_a.id,
            class_type_id=ct_yoga.id,
            name="Morning Hatha Flow",
            slug="morning-hatha-flow",
            short_description="Energizing 60-minute daily alignment and pranayama to elevate your day.",
            full_description="Start your mornings grounded and energized. This class blends traditional Surya Namaskar with focused hip-opening, core stability, and 10 minutes of cooling breathwork.",
            session_type=SessionType.GROUP.value,
            capacity=25,
            skill_level=SkillLevel.ALL_LEVELS.value,
            duration_minutes=60,
            language="English",
            is_public=True,
            meeting_url="https://meet.progressrooms.com/sahil-morning-flow"
        )
        db.add(s1)
        await db.flush()

        db.add(SessionInstructor(session_id=s1.id, member_id=mem_owner.id, role="PRIMARY"))
        db.add(SessionPricing(session_id=s1.id, pricing_type=PricingType.MONTHLY.value, price=2500.00, currency="INR", validity_days=30))
        db.add(SessionRequirement(
            session_id=s1.id,
            prerequisites="Open to all fitness levels. No prior yoga experience required.",
            required_equipment="Yoga mat, 2 yoga blocks, comfortable breathable attire.",
            preparation_instructions="Please avoid heavy meals 2 hours before class."
        ))
        db.add(SessionScheduleRule(
            session_id=s1.id,
            day_of_week=0, # Monday
            start_time=time(7, 0),
            end_time=time(8, 0),
            timezone="Asia/Kolkata",
            effective_start_date=now.date()
        ))
        db.add(SessionScheduleRule(
            session_id=s1.id,
            day_of_week=2, # Wednesday
            start_time=time(7, 0),
            end_time=time(8, 0),
            timezone="Asia/Kolkata",
            effective_start_date=now.date()
        ))
        db.add(SessionScheduleRule(
            session_id=s1.id,
            day_of_week=4, # Friday
            start_time=time(7, 0),
            end_time=time(8, 0),
            timezone="Asia/Kolkata",
            effective_start_date=now.date()
        ))

        # Session 2: Aerial Yoga
        s2 = Session(
            organization_id=org_a.id,
            class_type_id=ct_aerial.id,
            name="Aerial Yoga Conditioning",
            slug="aerial-yoga-conditioning",
            short_description="Suspended silks workout combining acrobatic strength and spinal decompression.",
            session_type=SessionType.GROUP.value,
            capacity=12,
            skill_level=SkillLevel.INTERMEDIATE.value,
            duration_minutes=60,
            language="English",
            is_public=True,
            meeting_url="https://meet.progressrooms.com/ananya-aerial"
        )
        db.add(s2)
        await db.flush()
        db.add(SessionInstructor(session_id=s2.id, member_id=mem_ananya.id, role="PRIMARY"))
        db.add(SessionPricing(session_id=s2.id, pricing_type=PricingType.MONTHLY.value, price=3500.00, currency="INR", validity_days=30))
        db.add(SessionRequirement(
            session_id=s2.id,
            prerequisites="Basic core strength recommended.",
            required_equipment="Suspension hammock (for online) or studio rig."
        ))
        db.add(SessionScheduleRule(
            session_id=s2.id,
            day_of_week=1, # Tuesday
            start_time=time(18, 0),
            end_time=time(19, 0),
            timezone="Asia/Kolkata",
            effective_start_date=now.date()
        ))
        db.add(SessionScheduleRule(
            session_id=s2.id,
            day_of_week=3, # Thursday
            start_time=time(18, 0),
            end_time=time(19, 0),
            timezone="Asia/Kolkata",
            effective_start_date=now.date()
        ))

        # 5. Students & Enrollments
        students = []
        for i in range(1, 21):
            s_user = User(email=f"student{i}@test.progressrooms.local", full_name=f"Student {i}")
            db.add(s_user)
            await db.flush()
            db.add(UserAuthMethod(user_id=s_user.id, auth_type="OTP", otp_code="123456", otp_expires_at=now + timedelta(days=365)))
            db.add(OrganizationStudent(organization_id=org_a.id, user_id=s_user.id, student_notes="Regular practitioner"))
            students.append(s_user)

        # Enroll student 1 in Morning Hatha Flow
        order1 = Order(
            organization_id=org_a.id,
            user_id=students[0].id,
            order_number="PR-TEST-ORD-001",
            total_amount=2500.00,
            currency="INR",
            status=OrderStatus.PAID.value
        )
        db.add(order1)
        await db.flush()

        db.add(OrderItem(order_id=order1.id, session_id=s1.id, unit_price=2500.00, quantity=1, subtotal=2500.00))
        db.add(Payment(
            order_id=order1.id,
            amount=2500.00,
            currency="INR",
            payment_provider="RAZORPAY",
            provider_payment_id="pay_mock_123456",
            provider_order_id="order_mock_123456",
            status=PaymentStatus.SUCCESS.value
        ))

        enr1 = SessionEnrollment(
            organization_id=org_a.id,
            session_id=s1.id,
            user_id=students[0].id,
            order_id=order1.id,
            status=EnrollmentStatus.ACTIVE.value,
            valid_from=now,
            valid_until=now + timedelta(days=30),
            total_sessions=12,
            sessions_attended=2
        )
        db.add(enr1)

        # Generate concrete occurrences for Org A
        # Occurrence 1: Upcoming tomorrow
        tomorrow_start = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
        occ_upcoming = ClassOccurrence(
            session_id=s1.id,
            organization_id=org_a.id,
            instructor_id=mem_owner.id,
            scheduled_start_at=tomorrow_start,
            scheduled_end_at=tomorrow_start + timedelta(hours=1),
            actual_start_at=tomorrow_start,
            actual_end_at=tomorrow_start + timedelta(hours=1),
            status=OccurrenceStatus.SCHEDULED.value,
            meeting_url="https://meet.progressrooms.com/sahil-morning-flow"
        )
        # Occurrence 2: Completed 2 days ago with attendance
        past_start = (now - timedelta(days=2)).replace(hour=7, minute=0, second=0, microsecond=0)
        occ_past = ClassOccurrence(
            session_id=s1.id,
            organization_id=org_a.id,
            instructor_id=mem_owner.id,
            scheduled_start_at=past_start,
            scheduled_end_at=past_start + timedelta(hours=1),
            actual_start_at=past_start,
            actual_end_at=past_start + timedelta(hours=1),
            status=OccurrenceStatus.COMPLETED.value
        )
        # Occurrence 3: Cancelled
        cancel_start = (now + timedelta(days=3)).replace(hour=7, minute=0, second=0, microsecond=0)
        occ_cancel = ClassOccurrence(
            session_id=s1.id,
            organization_id=org_a.id,
            instructor_id=mem_owner.id,
            scheduled_start_at=cancel_start,
            scheduled_end_at=cancel_start + timedelta(hours=1),
            actual_start_at=cancel_start,
            actual_end_at=cancel_start + timedelta(hours=1),
            status=OccurrenceStatus.CANCELLED.value,
            cancellation_reason="Teacher attending national yoga symposium"
        )
        db.add_all([occ_upcoming, occ_past, occ_cancel])
        await db.flush()

        db.add(AttendanceRecord(
            class_occurrence_id=occ_past.id,
            session_enrollment_id=enr1.id,
            status=AttendanceStatus.ATTENDED.value,
            marked_by_user_id=sahil_user.id,
            notes="Excellent posture adherence"
        ))

        # 6. Organization B: John Guitar Academy
        org_b = Organization(name="John Guitar Academy", slug="john-guitar-academy", timezone="Asia/Kolkata", currency="INR")
        db.add(org_b)
        await db.flush()

        john_user = User(email="studio@test.progressrooms.local", full_name="John Mayer")
        db.add(john_user)
        await db.flush()
        db.add(UserAuthMethod(user_id=john_user.id, auth_type="OTP", otp_code="123456", otp_expires_at=now + timedelta(days=365)))
        db.add(OrganizationMember(organization_id=org_b.id, user_id=john_user.id, role=UserRole.OWNER.value, title="Academy Director"))
        db.add(OrganizationSubscription(organization_id=org_b.id, plan_id=core_plan.id, status="ACTIVE", starts_at=now, ends_at=now + timedelta(days=365)))
        db.add(OrganizationWebsite(
            organization_id=org_b.id,
            template_id=tmpl_default.id,
            headline="Learn Acoustic & Electric Guitar from Masters",
            subheadline="Structured weekly lessons in rock, blues, fingerpicking, and music theory.",
            about_text="John Guitar Academy offers modern contemporary guitar instruction for all age groups."
        ))

        ct_guitar = ClassType(organization_id=org_b.id, name="Acoustic Guitar", slug="acoustic-guitar")
        db.add(ct_guitar)
        await db.flush()

        s_guitar = Session(
            organization_id=org_b.id,
            class_type_id=ct_guitar.id,
            name="Beginner Guitar Chords & Rhythms",
            slug="beginner-guitar-chords",
            short_description="Master open chords, strumming patterns, and your first 10 popular songs.",
            session_type=SessionType.GROUP.value,
            capacity=15,
            skill_level=SkillLevel.BEGINNER.value,
            duration_minutes=60,
            is_public=True
        )
        db.add(s_guitar)
        await db.flush()
        db.add(SessionPricing(session_id=s_guitar.id, pricing_type=PricingType.MONTHLY.value, price=2000.00, currency="INR"))

        # 7. Organization C: Premier Dance Studio
        org_c = Organization(name="Premier Dance Studio", slug="premier-dance-studio", timezone="Asia/Kolkata", currency="INR")
        db.add(org_c)
        await db.flush()

        dance_owner = User(email="dance.owner@test.progressrooms.local", full_name="Natasha Romanov")
        db.add(dance_owner)
        await db.flush()
        db.add(UserAuthMethod(user_id=dance_owner.id, auth_type="OTP", otp_code="123456", otp_expires_at=now + timedelta(days=365)))
        db.add(OrganizationMember(organization_id=org_c.id, user_id=dance_owner.id, role=UserRole.OWNER.value, title="Artistic Director"))
        db.add(OrganizationSubscription(organization_id=org_c.id, plan_id=studio_plan.id, status="ACTIVE", starts_at=now, ends_at=now + timedelta(days=365)))
        db.add(OrganizationWebsite(
            organization_id=org_c.id,
            template_id=tmpl_b.id, # Template B for Studio tier
            headline="Ignite Your Passion for Movement",
            subheadline="Contemporary, Hip-hop, and Classical Ballet training for aspiring performers.",
            about_text="Premier Dance Studio cultivates artistry, athletic precision, and creative expression."
        ))

        await db.commit()
        print("Database seeding completed successfully!")

if __name__ == "__main__":
    asyncio.run(seed_database())
