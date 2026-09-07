import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.core.database import AsyncSessionLocal
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.models.user import User, UserAuthMethod
from app.models.plan import Plan, OrganizationSubscription
from app.models.session import Session, ClassType
from app.common.enums import UserRole, OrgStatus

ADDITIONAL_STUDIOS = [
    {
        "name": "Zenith Aerial & Pilates Sanctuary",
        "slug": "zenith-aerial",
        "instructors": [
            {"email": "priya.aerial@zenith.com", "name": "Priya Nair", "title": "Lead Aerial Silks Artist & Coach", "role": "OWNER", "bio": "Over 10 years performing and coaching aerial acrobatics and restorative silks."},
            {"email": "karan.pilates@zenith.com", "name": "Karan Kapoor", "title": "Reformer & Mat Pilates Specialist", "role": "INSTRUCTOR", "bio": "Certified classical Pilates instructor specializing in postural rehabilitation."}
        ]
    },
    {
        "name": "Lotus Valley Wellness Academy",
        "slug": "lotus-valley",
        "instructors": [
            {"email": "elena.yoga@lotusvalley.com", "name": "Elena Rostova", "title": "Vinyasa & Ashtanga Master", "role": "OWNER", "bio": "500-hr RYT teacher leading mindful flow immersions worldwide."},
            {"email": "amitabh.sound@lotusvalley.com", "name": "Amitabh Das", "title": "Tibetan Sound Healing Practitioner", "role": "INSTRUCTOR", "bio": "Conducting vibrational sound bath journeys and meditative breathwork."}
        ]
    },
    {
        "name": "Prana Flow Movement Studio",
        "slug": "prana-flow",
        "instructors": [
            {"email": "maya.patel@pranaflow.com", "name": "Maya Patel", "title": "Hatha & Yin Restorative Teacher", "role": "OWNER", "bio": "Passionate about somatic trauma release and gentle yin flows."}
        ]
    }
]

async def seed_more():
    print("Seeding additional studios and instructors...")
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        starter_plan = (await db.execute(select(Plan).where(Plan.slug == "studio"))).scalar_one_or_none()
        if not starter_plan:
            starter_plan = (await db.execute(select(Plan))).scalars().first()

        for s_data in ADDITIONAL_STUDIOS:
            existing_org = (await db.execute(select(Organization).where(Organization.slug == s_data["slug"]))).scalar_one_or_none()
            if existing_org:
                print(f"Studio {s_data['slug']} exists, skipping.")
                continue

            org = Organization(name=s_data["name"], slug=s_data["slug"], timezone="Asia/Kolkata", currency="INR", status="ACTIVE")
            db.add(org)
            await db.flush()

            # Subscription
            if starter_plan:
                sub = OrganizationSubscription(
                    organization_id=org.id,
                    plan_id=starter_plan.id,
                    status="ACTIVE",
                    starts_at=now - timedelta(days=20),
                    payment_provider="RAZORPAY"
                )
                db.add(sub)

            # Instructors
            for inst_data in s_data["instructors"]:
                u = (await db.execute(select(User).where(User.email == inst_data["email"]))).scalar_one_or_none()
                if not u:
                    u = User(email=inst_data["email"], full_name=inst_data["name"])
                    db.add(u)
                    await db.flush()
                    db.add(UserAuthMethod(user_id=u.id, auth_type="OTP", otp_code="123456", otp_expires_at=now + timedelta(days=365)))

                member = OrganizationMember(
                    organization_id=org.id,
                    user_id=u.id,
                    role=inst_data["role"],
                    title=inst_data["title"],
                    bio=inst_data["bio"],
                    is_active=True
                )
                db.add(member)

        await db.commit()
        print("Successfully seeded additional studios and instructors!")

if __name__ == "__main__":
    asyncio.run(seed_more())
