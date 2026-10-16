import asyncio
import csv
import os
import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, and_, delete
from app.core.database import AsyncSessionLocal
from app.models.organization import Organization, OrganizationMember, OrganizationStudent
from app.models.user import User
from app.models.session import ClassType, Session, SessionInstructor
from app.models.schedule import ClassOccurrence
from app.models.commerce import SessionEnrollment
from app.models.attendance import AttendanceRecord, TeacherAttendance

CSV_OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "attendance_seed_august_2026.csv")
WEB_CSV_OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "progressrooms-web", "public", "data", "attendance_seed_august_2026.csv")

async def seed_attendance_data():
    async with AsyncSessionLocal() as s:
        # 1. Fetch Demo Studio and Rishi Yogi Studio
        demo_org = await s.scalar(select(Organization).where(Organization.slug == "demo-yoga-studio"))
        rishi_org = await s.scalar(select(Organization).where(Organization.slug.like("yogistudio%")))
        orgs = [o for o in [demo_org, rishi_org] if o is not None]
        if not orgs:
            print("No demo studio found!")
            return

        # 2. Fetch Teachers
        ananya_user = await s.scalar(select(User).where(User.email == "ananya@yogastudio.test"))
        rohit_user = await s.scalar(select(User).where(User.email == "rohit@yogastudio.test"))

        # 3. Fetch Students:
        # Ensure student1 (Aarav Patel) is index 0 (used for student dashboard dev-login),
        # and student347 (Aarav Bansal) is index 1 (default top student in studio dropdown).
        aarav_user = await s.scalar(select(User).where(User.email == "student1@test.progressrooms.local"))
        bansal_user = await s.scalar(select(User).where(User.email == "student347@test.progressrooms.local"))
        all_students = (await s.execute(select(User).where(User.role == "STUDENT").order_by(User.email))).scalars().all()
        
        filtered = [u for u in all_students if u.id not in {getattr(aarav_user, 'id', None), getattr(bansal_user, 'id', None)}]
        students = []
        if aarav_user:
            students.append(aarav_user)
        if bansal_user:
            students.append(bansal_user)
        students.extend(filtered)

        csv_records = []

        for org in orgs:
            print(f"Seeding attendance for organization: {org.name} ({org.slug})")
            zone = ZoneInfo(org.timezone or "Asia/Kolkata")
            
            # Find teacher members
            ananya_member = await s.scalar(select(OrganizationMember).where(
                OrganizationMember.organization_id == org.id, OrganizationMember.user_id == ananya_user.id))
            rohit_member = await s.scalar(select(OrganizationMember).where(
                OrganizationMember.organization_id == org.id, OrganizationMember.user_id == rohit_user.id))
            
            if not ananya_member:
                continue

            # Ensure top students are registered in OrganizationStudent for this studio
            for st in students[:10]:
                os_row = await s.scalar(select(OrganizationStudent).where(
                    OrganizationStudent.organization_id == org.id,
                    OrganizationStudent.user_id == st.id
                ))
                if not os_row:
                    s.add(OrganizationStudent(organization_id=org.id, user_id=st.id, status="ACTIVE"))
            await s.flush()

            # Class types
            ct = await s.scalar(select(ClassType).where(ClassType.organization_id == org.id))
            if not ct:
                ct = ClassType(organization_id=org.id, name="Yoga Sanctuary", slug=f"yoga-sanctuary-{org.id.hex[:6]}")
                s.add(ct)
                await s.flush()

            # Schedule plan definition for 30 days
            # Each entry: (day, time_str, session_title, teacher_member, student_assignments, teacher_status, is_future)
            # student_assignments is a list of (student_idx, student_status)
            plan = [
                # Day 1: Exactly 6 classes for first teacher (Ananya Sen)
                # Aarav Patel (idx 0) attends 4 classes; Aarav Bansal (idx 1) attends 4 classes
                (1, "07:00", "Vinyasa 1-on-1 Sunrise Flow", ananya_member, [(0, "ATTENDED"), (1, "ATTENDED")], "PRESENT", False),
                (1, "08:30", "Pranayama Alignment Session", ananya_member, [(0, "ATTENDED"), (2, "ATTENDED")], "PRESENT", False),
                (1, "10:00", "Hatha Flow 1-on-1 Practice", ananya_member, [(0, "ABSENT"), (1, "ABSENT")], "ABSENT", False),
                (1, "14:00", "Restorative Healing 1-on-1", ananya_member, [(1, "ATTENDED"), (3, "ATTENDED")], "PRESENT", False),
                (1, "16:30", "Yin Posture Check", ananya_member, [(0, "EXCUSED"), (4, "EXCUSED")], "CANCELLED", False),
                (1, "18:00", "Meditation & Breath Immersion", ananya_member, [(1, "ABSENT"), (5, "ABSENT")], "ABSENT", False),

                # Day 2: Exactly 6 classes for first teacher (Ananya Sen)
                (2, "07:00", "Ashtanga Core 1-on-1", ananya_member, [(0, "ATTENDED"), (1, "ATTENDED")], "PRESENT", False),
                (2, "08:30", "Backbend Mechanics 1-on-1", ananya_member, [(0, "ABSENT"), (6, "ABSENT")], "ABSENT", False),
                (2, "11:00", "Chakra Balance 1-on-1", ananya_member, [(0, "ATTENDED"), (7, "ATTENDED")], "PRESENT", False),
                (2, "14:30", "Spine Flexibility 1-on-1", ananya_member, [(1, "ABSENT"), (2, "ABSENT")], "ABSENT", False),
                (2, "16:00", "Breathwork Immersion Session", ananya_member, [(0, "EXCUSED"), (1, "EXCUSED")], "CANCELLED", False),
                (2, "17:30", "Evening Vinyasa 1-on-1", ananya_member, [(1, "ATTENDED"), (3, "ATTENDED")], "PRESENT", False),

                # Day 3 (Past)
                (3, "09:00", "Morning Gentle Hatha", ananya_member, [(0, "ABSENT"), (1, "ABSENT")], "ABSENT", False),
                (3, "17:00", "Power Flow & Balance", ananya_member, [(0, "ATTENDED"), (4, "ATTENDED")], "PRESENT", False),

                # Day 4 (Past)
                (4, "08:00", "Morning Vinyasa Surge", ananya_member, [(0, "ATTENDED"), (1, "ATTENDED")], "PRESENT", False),
                (4, "10:30", "Restorative Yin Deep", ananya_member, [(1, "ATTENDED"), (5, "ATTENDED")], "PRESENT", False),
                (4, "18:00", "Sound Meditation Session", ananya_member, [(0, None), (6, None)], None, False), # Unmarked (Yellow)

                # Day 5 (Past)
                (5, "07:30", "Pranayama Alignment Flow", ananya_member, [(0, "ABSENT"), (2, "ABSENT")], "ABSENT", False),
                (5, "16:30", "Yin Yoga Deep Stretch", ananya_member, [(1, "EXCUSED"), (3, "EXCUSED")], "CANCELLED", False),

                # Day 6 (Past)
                (6, "07:00", "Sunrise Ashtanga Series", ananya_member, [(0, "ABSENT"), (1, "ABSENT")], "ABSENT", False),
                (6, "09:00", "Vinyasa Core Stability", ananya_member, [(0, "ATTENDED"), (3, "ATTENDED")], "PRESENT", False),
                (6, "15:00", "Hatha Alignment Check", ananya_member, [(1, "ATTENDED"), (7, "ATTENDED")], "PRESENT", False),
                (6, "18:30", "Evening Breath Sanctuary", ananya_member, [(0, "ABSENT"), (4, "ABSENT")], "ABSENT", False),

                # Day 7 (Past)
                (7, "08:00", "Vinyasa Foundation Class", ananya_member, [(0, "ATTENDED"), (1, "ATTENDED")], "PRESENT", False),
                (7, "16:00", "Restorative Yoga Therapy", ananya_member, [(1, "EXCUSED"), (5, "EXCUSED")], "CANCELLED", False),

                # Day 8 (Past - up to today in test scenario)
                (8, "07:30", "Morning Ashtanga Flow", ananya_member, [(0, "EXCUSED"), (1, "EXCUSED")], "CANCELLED", False),
                (8, "11:00", "Spine Care 1-on-1 Clinic", ananya_member, [(0, "ATTENDED"), (3, "ATTENDED")], "PRESENT", False),
                (8, "17:00", "Twilight Meditation", ananya_member, [(1, "ABSENT"), (6, "ABSENT")], "ABSENT", False),

                # Day 9 to Day 30: FUTURE SCHEDULED CLASSES (Upcoming -> Blue mark!)
                # Day 10 (Future)
                (10, "08:00", "Vinyasa Core Masterclass", ananya_member, [(0, None), (1, None)], None, True),

                # Day 12 (Future)
                (12, "09:30", "Hatha Sun Salutations", ananya_member, [(0, None), (2, None)], None, True),
                (12, "17:30", "Evening Sound Sanctuary", ananya_member, [(1, None), (3, None)], None, True),

                # Day 15 (Future - Multiple 6 classes on a single day for Ananya Sen!)
                (15, "07:00", "1-on-1 Dawn Vinyasa", ananya_member, [(0, None), (1, None)], None, True),
                (15, "08:30", "1-on-1 Breathwork Workshop", ananya_member, [(2, None)], None, True),
                (15, "10:30", "1-on-1 Alignment Diagnostics", ananya_member, [(0, None), (3, None)], None, True),
                (15, "14:00", "1-on-1 Mobility & Flex", ananya_member, [(1, None)], None, True),
                (15, "16:00", "1-on-1 Restorative Rejuvenation", ananya_member, [(0, None), (4, None)], None, True),
                (15, "18:00", "1-on-1 Evening Yoga Nidra", ananya_member, [(1, None), (5, None)], None, True),

                # Day 18 (Future)
                (18, "08:00", "Morning Flow Immersion", ananya_member, [(6, None)], None, True),
                (18, "16:30", "Gentle Yin Sanctuary", ananya_member, [(7, None)], None, True),

                # Day 20 (Future - Multiple 5 classes on a single day for Ananya Sen!)
                (20, "07:00", "1-on-1 Sunrise Power Flow", ananya_member, [(0, None), (1, None)], None, True),
                (20, "09:00", "1-on-1 Core Conditioning", ananya_member, [(2, None)], None, True),
                (20, "11:00", "1-on-1 Flexibility Mastery", ananya_member, [(0, None), (3, None)], None, True),
                (20, "15:00", "1-on-1 Hip Opening Flow", ananya_member, [(1, None)], None, True),
                (20, "17:30", "1-on-1 Evening Pranayama", ananya_member, [(0, None), (4, None)], None, True),

                # Day 22 (Future)
                (22, "10:00", "Holistic Hatha Practice", ananya_member, [(5, None)], None, True),

                # Day 25 (Future - 3 classes)
                (25, "07:30", "Dynamic Vinyasa Flow", ananya_member, [(0, None)], None, True),
                (25, "12:00", "Noon Breath Reset", ananya_member, [(1, None), (6, None)], None, True),
                (25, "18:00", "Evening Yin Relaxation", ananya_member, [(7, None)], None, True),

                # Day 28 (Future - 2 classes)
                (28, "08:30", "Chakra Meditation Series", ananya_member, [(2, None)], None, True),
                (28, "16:00", "Restorative Posture Flow", ananya_member, [(3, None)], None, True),

                # Day 30 (Future - 4 classes)
                (30, "07:00", "Monthly Milestone Vinyasa", ananya_member, [(0, None), (1, None)], None, True),
                (30, "09:30", "1-on-1 Progress Review", ananya_member, [(3, None)], None, True),
                (30, "14:30", "1-on-1 Alignment Deep-Dive", ananya_member, [(4, None)], None, True),
                (30, "17:30", "Monthly Sunset Sanctuary", ananya_member, [(5, None)], None, True),
            ]

            # We seed for BOTH August 2026 (2026-08) and October 2026 (2026-10)
            months_to_seed = [("2026-08", 2026, 8), ("2026-10", 2026, 10)]

            for month_label, yr, mo in months_to_seed:
                print(f"  Generating month: {month_label} ...")
                for day, t_str, title, teacher, st_assignments, t_status, is_future in plan:
                    hour, minute = map(int, t_str.split(":"))
                    # Local start datetime in studio timezone
                    start_local = datetime(yr, mo, day, hour, minute, tzinfo=zone)
                    start_utc = start_local.astimezone(timezone.utc)
                    end_utc = start_utc + timedelta(hours=1)

                    # Create Session if needed
                    session_slug = f"{title.lower().replace(' ', '-').replace('&', 'and')}-{org.id.hex[:6]}"
                    session = await s.scalar(select(Session).where(Session.organization_id == org.id, Session.slug == session_slug))
                    if not session:
                        session = Session(
                            organization_id=org.id,
                            class_type_id=ct.id,
                            name=title,
                            slug=session_slug,
                            session_type="GROUP" if len(st_assignments) > 1 else "ONE_ON_ONE",
                            capacity=25 if len(st_assignments) > 1 else 1,
                            is_active=True
                        )
                        s.add(session)
                        await s.flush()
                        s.add(SessionInstructor(session_id=session.id, member_id=teacher.id, role="PRIMARY"))

                    # Create ClassOccurrence
                    occ = await s.scalar(select(ClassOccurrence).where(
                        ClassOccurrence.session_id == session.id,
                        ClassOccurrence.scheduled_start_at == start_utc
                    ))
                    occ_status = "SCHEDULED" if is_future else ("COMPLETED" if t_status != "CANCELLED" else "CANCELLED")
                    if not occ:
                        occ = ClassOccurrence(
                            organization_id=org.id,
                            session_id=session.id,
                            instructor_id=teacher.id,
                            scheduled_start_at=start_utc,
                            scheduled_end_at=end_utc,
                            actual_start_at=start_utc,
                            actual_end_at=end_utc,
                            status=occ_status,
                            meeting_url=f"https://meet.progressrooms.com/{uuid.uuid4()}"
                        )
                        s.add(occ)
                        await s.flush()
                    else:
                        occ.status = occ_status
                        occ.actual_start_at = start_utc
                        occ.scheduled_start_at = start_utc
                        occ.actual_end_at = end_utc
                        occ.scheduled_end_at = end_utc
                        occ.instructor_id = teacher.id

                    # Teacher Attendance
                    if not is_future:
                        if t_status:
                            t_rec = await s.scalar(select(TeacherAttendance).where(
                                TeacherAttendance.class_occurrence_id == occ.id,
                                TeacherAttendance.teacher_member_id == teacher.id
                            ))
                            if not t_rec:
                                t_rec = TeacherAttendance(
                                    class_occurrence_id=occ.id,
                                    teacher_member_id=teacher.id,
                                    status=t_status,
                                    notes="Verified practice session" if t_status == "PRESENT" else "Absence recorded"
                                )
                                s.add(t_rec)
                            else:
                                t_rec.status = t_status

                    # Student Enrollments & Attendance Records
                    for st_idx, st_status in st_assignments:
                        student = students[st_idx % len(students)]

                        enrollment = await s.scalar(select(SessionEnrollment).where(
                            SessionEnrollment.session_id == session.id,
                            SessionEnrollment.user_id == student.id
                        ))
                        if not enrollment:
                            enrollment = SessionEnrollment(
                                organization_id=org.id,
                                session_id=session.id,
                                user_id=student.id,
                                status="ACTIVE",
                                valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                valid_until=datetime(2026, 12, 31, tzinfo=timezone.utc),
                                total_sessions=100,
                                sessions_attended=1 if st_status == "ATTENDED" else 0
                            )
                            s.add(enrollment)
                            await s.flush()
                        else:
                            enrollment.status = "ACTIVE"
                            if enrollment.valid_from > start_utc:
                                enrollment.valid_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
                            if enrollment.valid_until and enrollment.valid_until < end_utc:
                                enrollment.valid_until = datetime(2026, 12, 31, tzinfo=timezone.utc)
                            if st_status == "ATTENDED":
                                enrollment.sessions_attended = (enrollment.sessions_attended or 0) + 1

                        if not is_future and st_status:
                            s_rec = await s.scalar(select(AttendanceRecord).where(
                                AttendanceRecord.class_occurrence_id == occ.id,
                                AttendanceRecord.session_enrollment_id == enrollment.id
                            ))
                            if not s_rec:
                                s_rec = AttendanceRecord(
                                    class_occurrence_id=occ.id,
                                    session_enrollment_id=enrollment.id,
                                    status=st_status,
                                    notes="Student attendance confirmed" if st_status == "ATTENDED" else "Missed scheduled session"
                                )
                                s.add(s_rec)
                            else:
                                s_rec.status = st_status

                        # Collect for CSV export if August and Demo Studio
                        if yr == 2026 and mo == 8 and org.id == demo_org.id:
                            csv_records.append({
                                "date": start_local.strftime("%Y-%m-%d"),
                                "time": start_local.strftime("%I:%M %p"),
                                "session_title": title,
                                "teacher_name": ananya_user.full_name,
                                "student_name": student.full_name,
                                "student_email": student.email,
                                "teacher_status": "SCHEDULED (Blue)" if is_future else (t_status or "UNMARKED (Yellow)"),
                                "student_status": "SCHEDULED (Blue)" if is_future else (st_status or "UNMARKED (Yellow)"),
                                "is_upcoming": "YES" if is_future else "NO",
                                "color_mark": "Blue" if is_future else ("Green" if (st_status or t_status) in ["PRESENT", "ATTENDED"] else ("Red" if (st_status or t_status) == "ABSENT" else "Yellow")),
                                "notes": f"Scenario entry for Day {day} ({student.full_name})"
                            })

        await s.commit()
        print("Database commit successful!")

        # Write CSV
        for path in [CSV_OUTPUT_PATH, WEB_CSV_OUTPUT_PATH]:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "date", "time", "session_title", "teacher_name", "student_name",
                    "student_email", "teacher_status", "student_status", "is_upcoming", "color_mark", "notes"
                ])
                writer.writeheader()
                writer.writerows(csv_records)
            print(f"Exported {len(csv_records)} attendance scenario rows to: {path}")

if __name__ == "__main__":
    asyncio.run(seed_attendance_data())
