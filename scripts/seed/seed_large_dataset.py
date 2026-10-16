import asyncio
import random
import uuid
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, delete, text
import sys, os
sys.path.insert(0, os.path.abspath("."))

from app.core.database import AsyncSessionLocal
from app.core.security import get_password_hash
from app.models.user import User
from app.models.organization import Organization, OrganizationMember, OrganizationStudent
from app.models.session import ClassType, Session, SessionPricing, SessionInstructor
from app.models.schedule import SessionScheduleRule, ClassOccurrence
from app.models.membership_tier import MembershipTier, MembershipTierClass
from app.models.commerce import SessionEnrollment, Order, OrderItem, Payment
from app.common.enums import UserRole, SessionType, SkillLevel, PricingType, InstructorRole, EnrollmentStatus, OrderStatus, PaymentStatus, OccurrenceStatus

FIRST_NAMES = [
    "Aarav", "Diya", "Rohan", "Tanya", "Siddharth", "Ananya", "Vikram", "Ishaan", "Neha", "Kabir",
    "Zoya", "Aryan", "Pooja", "Aditya", "Riya", "Karan", "Shruti", "Varun", "Meera", "Arjun",
    "Sneha", "Rahul", "Simran", "Naveen", "Kavya", "Akash", "Divya", "Gaurav", "Swati", "Manish",
    "Prerna", "Harsh", "Kritika", "Deepak", "Anjali", "Suresh", "Bhavna", "Alok", "Pallavi", "Nikhil",
    "Tarun", "Shreya", "Mayank", "Isha", "Abhinav", "Payal", "Vikas", "Tanvi", "Sunil", "Radhika",
    "Alex", "Emma", "Liam", "Sophia", "Noah", "Olivia", "Ethan", "Ava", "Lucas", "Mia"
]

LAST_NAMES = [
    "Sharma", "Patel", "Verma", "Iyer", "Sen", "Gupta", "Mehta", "Rao", "Nair", "Reddy",
    "Joshi", "Khan", "Kapoor", "Bhatia", "Malhotra", "Chopra", "Saxena", "Deshmukh", "Choudhury", "Menon",
    "Nambiar", "Banerjee", "Chatterjee", "Mishra", "Pandey", "Trivedi", "Kulkarni", "Aggarwal", "Bansal", "Singhal",
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis", "Wilson", "Anderson", "Taylor"
]

CITIES = ["Bangalore", "Mumbai", "Delhi", "Pune", "Hyderabad", "Chennai", "Kolkata", "Ahmedabad", "Jaipur", "Goa"]

TEACHERS_DATA = [
    {"name": "Ananya Sen", "email": "ananya@yogastudio.test", "username": "ananya_yoga", "code": "11000001", "title": "Aerial & Vinyasa Lead", "bio": "Master of aerial silks and dynamic restorative flow with 9 years of teaching."},
    {"name": "Rohit Verma", "email": "rohit@yogastudio.test", "username": "rohit_prana", "code": "11000002", "title": "Pranayama & Meditation Coach", "bio": "Himalayan breathwork specialist focusing on stress relief and autonomic regulation."},
    {"name": "Dr. Vikramaditya Rao", "email": "vikramaditya@yogastudio.test", "username": "vikram_ashtanga", "code": "11000003", "title": "Ashtanga Yoga & Philosophy Master", "bio": "PhD in Sanskrit traditions, teaching traditional Mysore-style Ashtanga yoga."},
    {"name": "Priya Swaminathan", "email": "priya.music@yogastudio.test", "username": "priya_vocals", "code": "11000004", "title": "Vocal Coach & Sound Artist", "bio": "Classical vocal coach and ear-training instructor with over 15 years in live performance."},
    {"name": "Marcus Chen", "email": "marcus.guitar@yogastudio.test", "username": "marcus_guitar", "code": "11000005", "title": "Guitar & Rhythm Specialist", "bio": "Fingerstyle, blues, and electric guitar instructor for beginners to touring musicians."},
    {"name": "Alistair Vance", "email": "alistair.venture@yogastudio.test", "username": "alistair_vc", "code": "11000006", "title": "Startup Fundraising & Pitch Mentor", "bio": "Ex-founder and angel investor coaching early-stage founders on storytelling and term sheets."},
    {"name": "Sarah Jenkins", "email": "sarah.growth@yogastudio.test", "username": "sarah_growth", "code": "11000007", "title": "Growth Marketing & SEO Strategist", "bio": "Hands-on growth practitioner helping creators and modern businesses acquire their first 10,000 customers."},
    {"name": "Meera Krishnan", "email": "meera.dance@yogastudio.test", "username": "meera_movement", "code": "11000008", "title": "Contemporary Movement Director", "bio": "Contemporary dancer and choreographer helping people rediscover joy and freedom in physical movement."},
    {"name": "Arjun Nambiar", "email": "arjun.calisthenics@yogastudio.test", "username": "arjun_strength", "code": "11000009", "title": "Calisthenics & Bodyweight Coach", "bio": "Gymnast and bodyweight strength athlete specializing in pull-ups, muscle-ups, and balance."},
    {"name": "Elena Rostova", "email": "elena.piano@yogastudio.test", "username": "elena_piano", "code": "11000010", "title": "Classical & Jazz Piano Faculty", "bio": "Concert pianist teaching chord theory, ear training, and expressive keyboard performance."},
    {"name": "Devika Sundaram", "email": "devika.design@yogastudio.test", "username": "devika_uiux", "code": "11000011", "title": "Product Design & Figma Lead", "bio": "Staff UI/UX designer teaching practical product design, design systems, and rapid prototyping."},
    {"name": "Rohan Kapoor", "email": "rohan.finance@yogastudio.test", "username": "rohan_finance", "code": "11000012", "title": "Financial Modeling & Valuation Instructor", "bio": "Investment banker teaching 3-statement financial models, SaaS metrics, and business forecasting."},
    {"name": "Chloe Dubois", "email": "chloe.writing@yogastudio.test", "username": "chloe_writer", "code": "11000013", "title": "Creative Writing & Narrative Craft", "bio": "Published author and scriptwriter helping creators craft compelling dialogue and emotional narratives."},
    {"name": "Tenzin Norbu", "email": "tenzin.sound@yogastudio.test", "username": "tenzin_bowls", "code": "11000014", "title": "Tibetan Singing Bowls & Sound Healer", "bio": "Sound therapist utilizing handcrafted singing bowls and gongs to induce theta-wave brain relaxation."}
]

CLASS_TYPES = [
    {"name": "Yoga & Movement", "slug": "yoga-movement", "description": "Holistic mind-body postures, alignment, and physical conditioning."},
    {"name": "Music & Audio", "slug": "music-audio", "description": "Instrument mastery, vocal agility, and music production."},
    {"name": "Business & Strategy", "slug": "business-strategy", "description": "Startup building, fundraising, financial modeling, and marketing."},
    {"name": "Tech & Product Design", "slug": "tech-product-design", "description": "UI/UX design, design systems, and software fundamentals."},
    {"name": "Creative Arts & Writing", "slug": "creative-arts", "description": "Visual illustration, screenplay writing, and storytelling."},
    {"name": "Fitness & Calisthenics", "slug": "fitness-calisthenics", "description": "Bodyweight strength, core conditioning, and athletic endurance."},
    {"name": "Mindfulness & Sound", "slug": "mindfulness-sound", "description": "Deep restorative meditation, breathwork, and sound therapy."}
]

CLASSES_DATA = [
    # Yoga & Movement
    {"type": "yoga-movement", "name": "Morning Hatha Flow", "slug": "morning-hatha-flow", "teacher": 0, "dur": 60, "cap": 25, "price": 2500, "days": [0, 2, 4], "time": "07:00", "desc": "Energizing 60-minute daily alignment and pranayama to elevate your day."},
    {"type": "yoga-movement", "name": "Aerial Yoga Silks & Inversions", "slug": "aerial-yoga-silks", "teacher": 0, "dur": 60, "cap": 12, "price": 3500, "days": [1, 3], "time": "18:00", "desc": "Suspended silks workout combining acrobatic strength and spinal decompression."},
    {"type": "yoga-movement", "name": "Ashtanga Primary Series Mysore", "slug": "ashtanga-primary-series", "teacher": 2, "dur": 75, "cap": 20, "price": 3200, "days": [0, 1, 2, 3], "time": "06:30", "desc": "Traditional sequential vinyasa practice building intense heat, focus, and stamina."},
    {"type": "yoga-movement", "name": "Yin Yoga & Deep Fascia Release", "slug": "yin-yoga-fascia-release", "teacher": 0, "dur": 60, "cap": 25, "price": 2200, "days": [2, 5], "time": "19:00", "desc": "Passive, long-held floor poses targeting connective tissue, ligaments, and joints."},
    {"type": "yoga-movement", "name": "Power Vinyasa Core Blast", "slug": "power-vinyasa-core", "teacher": 0, "dur": 45, "cap": 30, "price": 2000, "days": [1, 4], "time": "08:00", "desc": "Sweat-inducing athletic flow emphasizing arm balances, transitions, and core stability."},

    # Music & Audio
    {"type": "music-audio", "name": "Acoustic Guitar for Absolute Beginners", "slug": "acoustic-guitar-beginners", "teacher": 4, "dur": 60, "cap": 15, "price": 3000, "days": [1, 3], "time": "17:00", "desc": "Master basic chords, smooth strumming patterns, and your first 5 complete songs."},
    {"type": "music-audio", "name": "Fingerstyle Guitar & Melody Picking", "slug": "fingerstyle-guitar-melody", "teacher": 4, "dur": 60, "cap": 12, "price": 3500, "days": [5], "time": "11:00", "desc": "Advanced Travis picking, percussive slap guitar techniques, and dynamic arrangements."},
    {"type": "music-audio", "name": "Classical Piano Foundations", "slug": "classical-piano-foundations", "teacher": 9, "dur": 60, "cap": 10, "price": 3800, "days": [0, 3], "time": "16:00", "desc": "Learn sheet music reading, correct hand posture, and timeless piano etudes."},
    {"type": "music-audio", "name": "Jazz Piano & Chord Substitution", "slug": "jazz-piano-chords", "teacher": 9, "dur": 60, "cap": 10, "price": 4200, "days": [2, 5], "time": "18:00", "desc": "Explore 7th and 9th chords, modal vamps, and smooth jazz harmonic substitutions."},
    {"type": "music-audio", "name": "Vocal Range, Pitch & Breath Control", "slug": "vocal-range-pitch-control", "teacher": 3, "dur": 50, "cap": 15, "price": 3200, "days": [1, 4], "time": "17:30", "desc": "Unlock healthy head and chest resonance, eliminate throat strain, and expand your vocal range."},
    {"type": "music-audio", "name": "Music Production in Ableton Live", "slug": "music-production-ableton", "teacher": 3, "dur": 90, "cap": 15, "price": 4500, "days": [5], "time": "14:00", "desc": "From drum programming and audio warping to sidechain compression and final master bus processing."},

    # Business & Strategy
    {"type": "business-strategy", "name": "Startup Pitch Deck & Angel Fundraising", "slug": "startup-pitch-fundraising", "teacher": 5, "dur": 75, "cap": 25, "price": 5000, "days": [1, 3], "time": "19:00", "desc": "How to structure an investor narrative, build defensibility slides, and negotiate pre-seed valuation."},
    {"type": "business-strategy", "name": "Product Management: Idea to MVP", "slug": "product-management-mvp", "teacher": 5, "dur": 60, "cap": 25, "price": 4000, "days": [2, 4], "time": "18:30", "desc": "User story mapping, prioritizing backlogs with RICE, and running validated product discovery."},
    {"type": "business-strategy", "name": "Growth Marketing & SEO Playbook", "slug": "growth-marketing-seo", "teacher": 6, "dur": 60, "cap": 30, "price": 3500, "days": [0, 3], "time": "19:00", "desc": "Build high-converting landing pages, master topical authority SEO, and launch viral email funnels."},
    {"type": "business-strategy", "name": "Financial Modeling & SaaS Valuation in Excel", "slug": "financial-modeling-saas-excel", "teacher": 11, "dur": 90, "cap": 20, "price": 4800, "days": [5], "time": "10:00", "desc": "Build dynamic 3-statement forecast models, calculate CAC/LTV payback, and value businesses."},
    {"type": "business-strategy", "name": "Public Speaking & Executive Presence", "slug": "public-speaking-executive", "teacher": 5, "dur": 60, "cap": 15, "price": 3800, "days": [2], "time": "19:30", "desc": "Overcome stage anxiety, command any room, and articulate complex strategies with crisp poise."},
    {"type": "business-strategy", "name": "Freelance Business & Solo Agency Scaling", "slug": "freelance-agency-scaling", "teacher": 6, "dur": 60, "cap": 25, "price": 3200, "days": [4], "time": "18:00", "desc": "Transition from low-paying gigs to high-ticket retainers and productized client services."},

    # Tech & Product Design
    {"type": "tech-product-design", "name": "UI/UX Interface Design in Figma", "slug": "ui-ux-design-figma", "teacher": 10, "dur": 75, "cap": 20, "price": 4000, "days": [1, 3], "time": "17:30", "desc": "Learn typography scales, color theory, wireframing, and interactive micro-animations in Figma."},
    {"type": "tech-product-design", "name": "Design Systems & Component Architecture", "slug": "design-systems-components", "teacher": 10, "dur": 75, "cap": 15, "price": 4500, "days": [5], "time": "15:00", "desc": "Build scalable auto-layout token libraries, variants, and handoff specifications for engineering."},
    {"type": "tech-product-design", "name": "Web Development 101: HTML, CSS & React", "slug": "web-dev-html-css-react", "teacher": 10, "dur": 90, "cap": 25, "price": 4500, "days": [6], "time": "11:00", "desc": "A gentle, zero-jargon crash course on building and deploying live web applications."},

    # Creative Arts & Writing
    {"type": "creative-arts", "name": "Digital Illustration with Procreate", "slug": "digital-illustration-procreate", "teacher": 10, "dur": 60, "cap": 20, "price": 3000, "days": [2, 5], "time": "16:00", "desc": "Layer blending modes, custom brush creation, shading, and visual character illustration."},
    {"type": "creative-arts", "name": "Screenplay Writing & Cinematic Dialogue", "slug": "screenplay-cinematic-dialogue", "teacher": 12, "dur": 75, "cap": 15, "price": 3500, "days": [1, 4], "time": "19:00", "desc": "The 3-act story arc, subtext in character dialogue, and writing scenes that hold tension."},
    {"type": "creative-arts", "name": "Storytelling & Personal Memoir Craft", "slug": "personal-memoir-craft", "teacher": 12, "dur": 60, "cap": 15, "price": 3000, "days": [3], "time": "18:30", "desc": "Turn true lived experiences into deeply engaging essays and publishable personal essays."},

    # Fitness & Calisthenics
    {"type": "fitness-calisthenics", "name": "Calisthenics & Bodyweight Strength", "slug": "calisthenics-bodyweight-strength", "teacher": 8, "dur": 60, "cap": 20, "price": 2800, "days": [0, 2, 4], "time": "08:30", "desc": "Strict pull-up mechanics, dips, L-sits, and progressing toward your first muscle-up."},
    {"type": "fitness-calisthenics", "name": "Handstand & Inversion Balance Lab", "slug": "handstand-balance-lab", "teacher": 8, "dur": 60, "cap": 15, "price": 3200, "days": [5], "time": "09:00", "desc": "Wrist conditioning, shoulder flexion, wall-assisted drills, and freestanding handstand holds."},
    {"type": "fitness-calisthenics", "name": "Mat Pilates for Core & Postural Health", "slug": "mat-pilates-posture-health", "teacher": 7, "dur": 55, "cap": 25, "price": 2500, "days": [1, 3], "time": "09:30", "desc": "Classical Joseph Pilates repertoire strengthening the pelvic floor, lumbar spine, and obliques."},
    {"type": "fitness-calisthenics", "name": "Contemporary Dance & Freeflow Movement", "slug": "contemporary-dance-freeflow", "teacher": 7, "dur": 60, "cap": 20, "price": 2800, "days": [2, 5], "time": "17:00", "desc": "Expressive floor work, fluid weight shifts, and releasing physical tension through rhythm."},

    # Mindfulness & Sound
    {"type": "mindfulness-sound", "name": "Himalayan Breathwork & Pranayama", "slug": "himalayan-breathwork-pranayama", "teacher": 1, "dur": 45, "cap": 35, "price": 1800, "days": [0, 2, 4], "time": "06:15", "desc": "Kapalabhati, Nadi Shodhana, and Bhastrika to oxygenate cells and calm cognitive restlessness."},
    {"type": "mindfulness-sound", "name": "Tibetan Singing Bowls & Sound Bath", "slug": "tibetan-bowls-sound-bath", "teacher": 13, "dur": 60, "cap": 30, "price": 2200, "days": [2, 6], "time": "20:00", "desc": "Bathe in harmonic resonance from handcrafted 7-metal bowls to promote deep theta healing."},
    {"type": "mindfulness-sound", "name": "Yoga Nidra & Guided Deep Sleep Meditation", "slug": "yoga-nidra-deep-sleep", "teacher": 1, "dur": 45, "cap": 40, "price": 1500, "days": [6], "time": "21:00", "desc": "The ancient art of conscious yogic sleep to reset the central nervous system and restore vitality."},
    {"type": "mindfulness-sound", "name": "Chakra Alignment & Sonic Vibration", "slug": "chakra-sonic-vibration", "teacher": 13, "dur": 60, "cap": 25, "price": 2400, "days": [3], "time": "19:30", "desc": "Solfeggio frequency meditation aligning energetic centers through acoustic vibration."}
]

MEMBERSHIPS_DATA = [
    {"name": "Monthly Unlimited All-Access Pass", "slug": "monthly-unlimited-all-access", "type": "MONTHLY", "price": 5999, "offer": 4999, "validity": 30, "classes": None, "desc": "Unlimited access to all live Yoga, Music, Business, Art, and Calisthenics sessions."},
    {"name": "10-Class Flexible Pass", "slug": "10-class-flexible-pass", "type": "PACKAGE", "price": 3499, "offer": 2999, "validity": 60, "classes": 10, "desc": "10 sessions valid for any group class across the entire sanctuary schedule."},
    {"name": "5-Class Discovery Pack", "slug": "5-class-discovery-pack", "type": "PACKAGE", "price": 1999, "offer": 1749, "validity": 30, "classes": 5, "desc": "A 5-class exploration card across multiple disciplines."},
    {"name": "Single Session Drop-In Ticket", "slug": "single-session-drop-in", "type": "DROP_IN", "price": 500, "offer": 450, "validity": 7, "classes": 1, "desc": "Single admission ticket for any upcoming live class of your choice."},
    {"name": "Morning Yoga & Meditation Monthly", "slug": "morning-yoga-meditation-monthly", "type": "MONTHLY", "price": 2999, "offer": 2499, "validity": 30, "classes": 20, "desc": "Daily morning access to Hatha Flow, Ashtanga, and Himalayan Breathwork sessions."},
    {"name": "Sound Bath & Restorative Healing 4-Pack", "slug": "sound-bath-restoration-4pack", "type": "PACKAGE", "price": 1800, "offer": 1500, "validity": 45, "classes": 4, "desc": "4 evenings of Tibetan singing bowls, gong immersion, and guided Yoga Nidra."},
    {"name": "Aerial Silks & Inversions 6-Session Card", "slug": "aerial-silks-inversions-6pack", "type": "PACKAGE", "price": 3200, "offer": 2800, "validity": 45, "classes": 6, "desc": "6 specialized sessions on suspended hammocks and inversion alignment."},
    {"name": "Music Academy Monthly - 8 Sessions", "slug": "music-academy-monthly-8pack", "type": "MONTHLY", "price": 4500, "offer": 3900, "validity": 30, "classes": 8, "desc": "8 classes per month across Guitar, Piano, and Vocal Coaching tracks."},
    {"name": "Guitar Mastery 12-Session Pass", "slug": "guitar-mastery-12pack", "type": "PACKAGE", "price": 5400, "offer": 4600, "validity": 60, "classes": 12, "desc": "Dedicated training in acoustic, fingerstyle, and lead guitar techniques."},
    {"name": "Piano & Keyboard Theory 8-Pack", "slug": "piano-keyboard-theory-8pack", "type": "PACKAGE", "price": 4800, "offer": 4200, "validity": 60, "classes": 8, "desc": "8 comprehensive piano lessons covering classical sheet music to jazz harmony."},
    {"name": "Vocal Agility & Ear Training 10-Pack", "slug": "vocal-agility-10pack", "type": "PACKAGE", "price": 5000, "offer": 4300, "validity": 45, "classes": 10, "desc": "Intensive coaching on pitch control, breathing, and performance confidence."},
    {"name": "Founder & Entrepreneur Full Access Pass", "slug": "founder-entrepreneur-pass", "type": "PACKAGE", "price": 7500, "offer": 6500, "validity": 90, "classes": 20, "desc": "Full curriculum access to Pitch Deck, Product Management, Growth SEO, and Excel Modeling."},
    {"name": "Startup Pitch & Growth Sprint (6 Classes)", "slug": "startup-pitch-growth-sprint", "type": "PACKAGE", "price": 3999, "offer": 3499, "validity": 30, "classes": 6, "desc": "6 targeted sessions for founders preparing to raise capital and launch their product."},
    {"name": "Design & Figma Bootcamp Pass", "slug": "design-figma-bootcamp-pass", "type": "PACKAGE", "price": 4200, "offer": 3600, "validity": 45, "classes": 8, "desc": "8 classes covering UI/UX design, auto-layout components, and portfolio critique."},
    {"name": "Creative Writing & Screenplay Circle", "slug": "creative-writing-screenplay-pass", "type": "PACKAGE", "price": 3000, "offer": 2600, "validity": 30, "classes": 6, "desc": "6 collaborative workshops developing screenplays, dialogue, and personal essays."},
    {"name": "Procreate & Digital Art 8-Pack", "slug": "procreate-digital-art-8pack", "type": "PACKAGE", "price": 3600, "offer": 3100, "validity": 60, "classes": 8, "desc": "8 hands-on illustration classes exploring lighting, brushes, and character drawing."},
    {"name": "Calisthenics & Bodyweight 12-Pack", "slug": "calisthenics-bodyweight-12pack", "type": "PACKAGE", "price": 3800, "offer": 3300, "validity": 45, "classes": 12, "desc": "12 intense coaching sessions on pull-ups, dips, handstand balance, and core strength."},
    {"name": "Mat Pilates & Core Health Pass", "slug": "mat-pilates-core-health-pass", "type": "MONTHLY", "price": 3200, "offer": 2700, "validity": 30, "classes": 8, "desc": "8 reformer-inspired mat Pilates classes to relieve back tension and tone stabilizing muscles."},
    {"name": "Weekend Warrior All-Studio Pass", "slug": "weekend-warrior-pass", "type": "PACKAGE", "price": 2499, "offer": 2199, "validity": 30, "classes": 6, "desc": "Access to any Saturday or Sunday sessions across movement, music, and art."},
    {"name": "Annual VIP Sanctuary Fellowship", "slug": "annual-vip-sanctuary-fellowship", "type": "MONTHLY", "price": 45000, "offer": 38000, "validity": 365, "classes": None, "desc": "Full 365-day unlimited VIP membership with private invitations to workshops and retreats."},
    {"name": "Student Discovery 3-Class Trial", "slug": "student-discovery-3class-trial", "type": "PACKAGE", "price": 999, "offer": 799, "validity": 14, "classes": 3, "desc": "Special promotional 3-class pass for new sanctuary members to sample any offering."},
    {"name": "Corporate Wellness 20-Pack", "slug": "corporate-wellness-20pack", "type": "PACKAGE", "price": 8999, "offer": 7999, "validity": 90, "classes": 20, "desc": "Shareable 20-class wellness package for teams to join yoga, breathwork, and sound baths."}
]

async def seed_organization_data(db, org):
    now = datetime.now(timezone.utc)
    print(f"\n==========================================")
    print(f"Targeting studio: {org.name} ({org.slug}, id: {org.id})")
    print(f"==========================================")

    # 1. Seed or update Teachers (14 instructors)
    print("Seeding 14 teachers...")
    teacher_members = []
    for t_info in TEACHERS_DATA:
        user = await db.scalar(select(User).where(User.email == t_info["email"]))
        if not user:
            user = User(
                email=t_info["email"],
                username=t_info["username"],
                full_name=t_info["name"],
                role="TEACHER",
                phone=f"+91 98765 {random.randint(10000, 99999)}",
                address=f"{random.randint(10, 99)}, Sanctuary Boulevard",
                location="Bangalore, India",
                password_hash=get_password_hash("teacher123"),
                is_verified=True,
                account_status="ACTIVE"
            )
            db.add(user)
            await db.flush()


        member = await db.scalar(select(OrganizationMember).where(
            OrganizationMember.organization_id == org.id,
            OrganizationMember.user_id == user.id
        ))
        if not member:
            target_code = t_info["code"]
            code_in_use = await db.scalar(select(OrganizationMember.id).where(OrganizationMember.teacher_code == target_code))
            if code_in_use:
                while True:
                    candidate = f"{random.randint(10000000, 99999999)}"
                    if not await db.scalar(select(OrganizationMember.id).where(OrganizationMember.teacher_code == candidate)):
                        target_code = candidate
                        break

            member = OrganizationMember(
                organization_id=org.id,
                user_id=user.id,
                role="TEACHER",
                title=t_info["title"],
                bio=t_info["bio"],
                teacher_code=target_code,
                is_active=True
            )
            db.add(member)
            await db.flush()
        else:
            if not member.teacher_code:
                target_code = t_info["code"]
                code_in_use = await db.scalar(select(OrganizationMember.id).where(OrganizationMember.teacher_code == target_code))
                if code_in_use:
                    while True:
                        candidate = f"{random.randint(10000000, 99999999)}"
                        if not await db.scalar(select(OrganizationMember.id).where(OrganizationMember.teacher_code == candidate)):
                            target_code = candidate
                            break
                member.teacher_code = target_code
            member.title = t_info["title"]
            member.bio = t_info["bio"]
            member.is_active = True

        teacher_members.append(member)

    print(f"Teachers seeded: {len(teacher_members)}")

    # 2. Seed Class Types
    print("Seeding Class Types...")
    type_map = {}
    for ct_data in CLASS_TYPES:
        ct = await db.scalar(select(ClassType).where(
            ClassType.organization_id == org.id,
            ClassType.slug == ct_data["slug"]
        ))
        if not ct:
            ct = ClassType(
                organization_id=org.id,
                name=ct_data["name"],
                slug=ct_data["slug"],
                description=ct_data["description"]
            )
            db.add(ct)
            await db.flush()
        type_map[ct_data["slug"]] = ct

    # 3. Seed Classes / Sessions (31 classes)
    print("Seeding 31 Classes...")
    sessions_created = []
    for c_data in CLASSES_DATA:
        ct = type_map[c_data["type"]]
        teacher_member = teacher_members[c_data["teacher"] % len(teacher_members)]
        s = await db.scalar(select(Session).where(
            Session.organization_id == org.id,
            Session.slug == c_data["slug"]
        ))
        t_name = TEACHERS_DATA[c_data["teacher"] % len(TEACHERS_DATA)]["name"]
        if not s:
            s = Session(
                organization_id=org.id,
                class_type_id=ct.id,
                name=c_data["name"],
                slug=c_data["slug"],
                short_description=c_data["desc"],
                full_description=f"{c_data['desc']} An inspiring journey led by expert instructor {t_name}.",
                session_type=SessionType.GROUP.value,
                capacity=c_data["cap"],
                duration_minutes=c_data["dur"],
                language="English",
                is_active=True,
                is_public=True,
                meeting_url=f"https://meet.progressrooms.com/{c_data['slug']}"
            )
            db.add(s)
            await db.flush()

            # Pricing
            db.add(SessionPricing(
                session_id=s.id,
                pricing_type=PricingType.MONTHLY.value,
                price=c_data["price"],
                currency="INR",
                validity_days=30
            ))

            # Instructor
            db.add(SessionInstructor(
                session_id=s.id,
                member_id=teacher_member.id,
                role="PRIMARY"
            ))

            # Schedule Rules & Occurrences
            start_h, start_m = map(int, c_data["time"].split(":"))
            start_t = time(start_h, start_m)
            end_t = time((start_h + (start_m + c_data["dur"]) // 60) % 24, (start_m + c_data["dur"]) % 60)

            for day_num in c_data["days"]:
                db.add(SessionScheduleRule(
                    session_id=s.id,
                    day_of_week=day_num,
                    start_time=start_t,
                    end_time=end_t,
                    timezone="Asia/Kolkata",
                    effective_start_date=now.date() - timedelta(days=14)
                ))

            # Create occurrences for past 7 days and next 14 days
            base_date = now.date() - timedelta(days=7)
            for day_offset in range(22):
                target_date = base_date + timedelta(days=day_offset)
                if target_date.weekday() in c_data["days"]:
                    studio_tz = ZoneInfo("Asia/Kolkata")
                    dt_start_local = datetime.combine(target_date, start_t, tzinfo=studio_tz)
                    dt_start = dt_start_local.astimezone(timezone.utc)
                    end_date = target_date + timedelta(days=1) if end_t < start_t else target_date
                    dt_end_local = datetime.combine(end_date, end_t, tzinfo=studio_tz)
                    dt_end = dt_end_local.astimezone(timezone.utc)
                    occ_status = OccurrenceStatus.COMPLETED.value if target_date < now.date() else OccurrenceStatus.SCHEDULED.value
                    occ = ClassOccurrence(
                        session_id=s.id,
                        organization_id=org.id,
                        instructor_id=teacher_member.id,
                        scheduled_start_at=dt_start,
                        scheduled_end_at=dt_end,
                        actual_start_at=dt_start,
                        actual_end_at=dt_end,
                        status=occ_status,
                        meeting_url=s.meeting_url
                    )
                    db.add(occ)

        sessions_created.append(s)

    print(f"Classes ready: {len(sessions_created)}")

    # 4. Seed Membership Tiers (22 tiers)
    print("Seeding 22 Membership Tiers...")
    for tier_data in MEMBERSHIPS_DATA:
        mt = await db.scalar(select(MembershipTier).where(
            MembershipTier.organization_id == org.id,
            MembershipTier.slug == tier_data["slug"]
        ))
        if not mt:
            mt = MembershipTier(
                organization_id=org.id,
                name=tier_data["name"],
                slug=tier_data["slug"],
                tier_type=tier_data["type"],
                price=tier_data["price"],
                offer_price=tier_data["offer"],
                currency="INR",
                validity_days=tier_data["validity"],
                total_classes=tier_data["classes"],
                description=tier_data["desc"],
                is_active=True
            )
            db.add(mt)
            await db.flush()

            # Attach relevant classes to this tier based on slug
            tier_mapping = {
                'monthly-unlimited-all-access': '__ALL__',
                '10-class-flexible-pass': ['morning-hatha-flow', 'aerial-yoga-silks', 'ashtanga-primary-series', 'yin-yoga-fascia-release', 'power-vinyasa-core', 'calisthenics-bodyweight-strength', 'mat-pilates-posture-health', 'himalayan-breathwork-pranayama', 'tibetan-bowls-sound-bath'],
                '5-class-discovery-pack': ['morning-hatha-flow', 'acoustic-guitar-beginners', 'calisthenics-bodyweight-strength', 'tibetan-bowls-sound-bath', 'ui-ux-design-figma'],
                'single-session-drop-in': '__ALL__',
                'morning-yoga-meditation-monthly': ['morning-hatha-flow', 'ashtanga-primary-series', 'himalayan-breathwork-pranayama', 'yin-yoga-fascia-release', 'power-vinyasa-core'],
                'sound-bath-restoration-4pack': ['tibetan-bowls-sound-bath', 'yoga-nidra-deep-sleep', 'chakra-sonic-vibration', 'himalayan-breathwork-pranayama'],
                'aerial-silks-inversions-6pack': ['aerial-yoga-silks', 'power-vinyasa-core', 'handstand-balance-lab'],
                'music-academy-monthly-8pack': ['acoustic-guitar-beginners', 'fingerstyle-guitar-melody', 'classical-piano-foundations', 'jazz-piano-chords', 'vocal-range-pitch-control', 'music-production-ableton'],
                'guitar-mastery-12pack': ['acoustic-guitar-beginners', 'fingerstyle-guitar-melody'],
                'piano-keyboard-theory-8pack': ['classical-piano-foundations', 'jazz-piano-chords'],
                'vocal-agility-10pack': ['vocal-range-pitch-control', 'music-production-ableton'],
                'founder-entrepreneur-pass': ['startup-pitch-fundraising', 'product-management-mvp', 'growth-marketing-seo', 'financial-modeling-saas-excel', 'public-speaking-executive', 'freelance-agency-scaling'],
                'startup-pitch-growth-sprint': ['startup-pitch-fundraising', 'growth-marketing-seo', 'product-management-mvp'],
                'design-figma-bootcamp-pass': ['ui-ux-design-figma', 'design-systems-components', 'web-dev-html-css-react'],
                'creative-writing-screenplay-pass': ['screenplay-cinematic-dialogue', 'personal-memoir-craft', 'digital-illustration-procreate'],
                'procreate-digital-art-8pack': ['digital-illustration-procreate', 'ui-ux-design-figma'],
                'calisthenics-bodyweight-12pack': ['calisthenics-bodyweight-strength', 'handstand-balance-lab', 'mat-pilates-posture-health'],
                'mat-pilates-core-health-pass': ['mat-pilates-posture-health', 'contemporary-dance-freeflow', 'power-vinyasa-core'],
                'weekend-warrior-pass': ['power-vinyasa-core', 'calisthenics-bodyweight-strength', 'tibetan-bowls-sound-bath', 'handstand-balance-lab', 'music-production-ableton'],
                'annual-vip-sanctuary-fellowship': '__ALL__',
                'student-discovery-3class-trial': ['morning-hatha-flow', 'calisthenics-bodyweight-strength', 'tibetan-bowls-sound-bath', 'acoustic-guitar-beginners'],
                'corporate-wellness-20pack': ['morning-hatha-flow', 'yin-yoga-fascia-release', 'himalayan-breathwork-pranayama', 'tibetan-bowls-sound-bath', 'yoga-nidra-deep-sleep']
            }
            target_slugs = tier_mapping.get(mt.slug, '__ALL__')
            if target_slugs == '__ALL__':
                attached_sessions = sessions_created
            else:
                attached_sessions = [s for s in sessions_created if s.slug in target_slugs]

            for session_item in attached_sessions:
                db.add(MembershipTierClass(tier_id=mt.id, session_id=session_item.id))

    print("Membership tiers seeded.")

    # 5. Seed 500 Students
    print("Checking existing students count...")
    existing_students_count = await db.scalar(select(text("count(*)")).select_from(OrganizationStudent).where(OrganizationStudent.organization_id == org.id)) or 0
    print(f"Current students for {org.slug}: {existing_students_count}")

    needed = max(0, 500 - existing_students_count)
    if needed > 0:
        print(f"Adding {needed} students to reach ~500...")
        created_students = []
        start_num = existing_students_count + 1
        for i in range(start_num, start_num + needed):
            fname = random.choice(FIRST_NAMES)
            lname = random.choice(LAST_NAMES)
            city = random.choice(CITIES)
            email = f"student_{org.slug[:6]}_{i}@test.progressrooms.local"

            existing_user = await db.scalar(select(User).where(User.email == email))
            if not existing_user:
                user = User(
                    email=email,
                    full_name=f"{fname} {lname}",
                    role="STUDENT",
                    phone=f"+91 98765 {random.randint(10000, 99999)}",
                    whatsapp_number=f"+91 91234 {random.randint(10000, 99999)}",
                    location=f"{city}, India",
                    address=f"{random.randint(1, 150)}, 4th Cross, {city}",
                    is_verified=True,
                    account_status="ACTIVE",
                    created_at=now - timedelta(days=random.randint(1, 180))
                )
                db.add(user)
                await db.flush()

            else:
                user = existing_user

            status = "ACTIVE" if random.random() < 0.85 else ("INACTIVE" if random.random() < 0.7 else "SUSPENDED")
            assigned_teacher = random.choice(teacher_members) if random.random() < 0.4 else None

            org_student = await db.scalar(select(OrganizationStudent).where(
                OrganizationStudent.organization_id == org.id,
                OrganizationStudent.user_id == user.id
            ))
            if not org_student:
                org_student = OrganizationStudent(
                    organization_id=org.id,
                    user_id=user.id,
                    teacher_member_id=assigned_teacher.id if assigned_teacher else None,
                    student_notes=f"Enthusiastic learner from {city}. Enjoys interactive workshops.",
                    status=status,
                    created_at=user.created_at
                )
                db.add(org_student)

            created_students.append(user)

            if len(created_students) % 50 == 0:
                await db.flush()
                print(f"  Flushed {len(created_students)} students...")

        await db.flush()
        print(f"Successfully added {len(created_students)} students!")

    # 6. Enroll students
    print("Ensuring active student enrollments...")
    enrollment_count = await db.scalar(select(text("count(*)")).select_from(SessionEnrollment).where(SessionEnrollment.organization_id == org.id)) or 0
    if enrollment_count < 100:
        all_org_students = (await db.execute(select(OrganizationStudent).where(
            OrganizationStudent.organization_id == org.id,
            OrganizationStudent.status == "ACTIVE"
        ).limit(200))).scalars().all()

        for idx, org_student in enumerate(all_org_students):
            session_to_enroll = sessions_created[idx % len(sessions_created)]
            existing_enr = await db.scalar(select(SessionEnrollment).where(
                SessionEnrollment.organization_id == org.id,
                SessionEnrollment.session_id == session_to_enroll.id,
                SessionEnrollment.user_id == org_student.user_id
            ))
            if not existing_enr:
                attended = random.randint(0, 10)
                total_s = random.choice([10, 12, 16, 20])
                enr = SessionEnrollment(
                    organization_id=org.id,
                    session_id=session_to_enroll.id,
                    user_id=org_student.user_id,
                    status=EnrollmentStatus.ACTIVE.value,
                    valid_from=now - timedelta(days=random.randint(5, 25)),
                    valid_until=now + timedelta(days=random.randint(15, 45)),
                    total_sessions=total_s,
                    sessions_attended=min(attended, total_s),
                    billing_value=random.choice([2500.0, 3200.0, 4500.0]),
                    billing_currency="INR",
                    billing_type="PACKAGE",
                    billing_label=f"Class Pack - {session_to_enroll.name}"
                )
                db.add(enr)

        await db.flush()
        print("Student enrollments populated.")

    await db.commit()
    print(f"SEEDING FOR {org.name} COMPLETED SUCCESSFULLY!\n")

async def seed_large_dataset():
    async with AsyncSessionLocal() as db:
        studios_to_seed = ["demo-yoga-studio", "yogistudio-886070"]
        for slug in studios_to_seed:
            org = await db.scalar(select(Organization).where(Organization.slug == slug))
            if org:
                await seed_organization_data(db, org)
        print("ALL TARGET STUDIOS SEEDED SUCCESSFULLY!")

if __name__ == "__main__":
    asyncio.run(seed_large_dataset())
