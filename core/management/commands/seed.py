from django.core.management.base import BaseCommand
from datetime import date, timedelta
from core.models import (
    User, Booking, CreditTransaction, Receipt, AvailabilityOverride,
    CustomTime, StudentNote, ActiveLesson, SiteSettings
)


class Command(BaseCommand):
    help = "Seed the database with initial data"

    def handle(self, *args, **options):
        self.stdout.write("Clearing existing data...")
        # Clear in safe order
        ActiveLesson.objects.all().delete()
        StudentNote.objects.all().delete()
        CreditTransaction.objects.all().delete()
        Receipt.objects.all().delete()
        Booking.objects.all().delete()
        AvailabilityOverride.objects.all().delete()
        CustomTime.objects.all().delete()
        SiteSettings.objects.all().delete()
        User.objects.all().delete()

        self.stdout.write("Creating users...")

        # Students
        maya = User.objects.create_user(
            username="maya", email="maya@fluent.at", password="password",
            role="student", slug="maya", initials="MK",
            # Starting grant; the 6 bookings created below each debit one credit
            # (Booking.save() now charges on creation), leaving the demo balance at 8.
            credits=14, color1="#309050", color2="#277a42",
            first_name="Maya", last_name="Karlsson",
            billing_name="Maya Karlsson",
            billing_line1="Mariahilfer Straße 45/12",
            billing_postcode="1060", billing_city="Wien", billing_country="Österreich",
            receipt_seq=1010,
        )
        theo = User.objects.create_user(
            username="theo", email="theo@fluent.at", password="password",
            role="student", slug="theo", initials="TN",
            credits=5, color1="#4cb56b", color2="#3a8f52",  # 2 bookings -> balance 3
            first_name="Theo", last_name="Nguyen",
            billing_name="Theo Nguyen",
            billing_line1="Praterstraße 8/3",
            billing_postcode="1020", billing_city="Wien", billing_country="Österreich",
            receipt_seq=1020,
        )
        ines = User.objects.create_user(
            username="ines", email="ines@fluent.at", password="password",
            role="student", slug="ines", initials="IR",
            credits=1, color1="#7cb342", color2="#5a9e3f",  # 1 booking -> balance 0
            first_name="Inés", last_name="Romero",
            billing_name="Inés Romero",
            billing_line1="Getreidegasse 21",
            billing_postcode="5020", billing_city="Salzburg", billing_country="Österreich",
            receipt_seq=1030,
        )
        omar = User.objects.create_user(
            username="omar", email="omar@fluent.at", password="password",
            role="student", slug="omar", initials="OH",
            credits=15, color1="#52a86a", color2="#2f8a4d",  # 3 bookings -> balance 12
            first_name="Omar", last_name="Haddad",
            billing_name="Omar Haddad",
            billing_line1="Herrengasse 12",
            billing_postcode="8010", billing_city="Graz", billing_country="Österreich",
            receipt_seq=1040,
        )
        lena = User.objects.create_user(
            username="lena", email="lena@fluent.at", password="password",
            role="student", slug="lena", initials="LF",
            credits=2, color1="#6bbf86", color2="#3f9a5e",  # 1 booking -> balance 1
            first_name="Lena", last_name="Fischer",
            billing_name="Lena Fischer",
            billing_line1="Maria-Theresien-Str. 18",
            billing_postcode="6020", billing_city="Innsbruck", billing_country="Österreich",
            receipt_seq=1050,
        )

        # Tutor
        davit = User.objects.create_user(
            username="davit", email="davit@fluent.at", password="password",
            role="tutor", slug="davit", initials="DV",
            color1="#309050", color2="#277a42",
            first_name="Davit", last_name="Petrosyan",
            receipt_seq=1000,
        )

        # Admin
        User.objects.create_user(
            username="admin", email="admin@fluent.at", password="password",
            role="admin", slug="admin", initials="AD",
            color1="#3aa55c", color2="#277a42",
            first_name="Studio", last_name="Admin",
            receipt_seq=1000,
        )
        self.stdout.write("Creating bookings...")

        week_start = date(2026, 6, 1)  # a Monday

        def wd(offset):
            """Return date offset days from week_start."""
            return week_start + timedelta(days=offset)

        next_week_start = week_start + timedelta(days=7)

        # Upcoming bookings
        Booking.objects.create(student=maya, tutor=davit, date=wd(0), time="15:30", title="Business English")
        Booking.objects.create(student=maya, tutor=davit, date=wd(1), time="10:30", title="Conversation practice")
        Booking.objects.create(
            student=maya, tutor=davit, date=wd(3), time="14:00",
            title="IELTS speaking mock",
            notes="Focus on Part 2 long-turn fluency; bring 3 cue cards.",
            tutor_notes="Last session: hesitant with linking words. Revisit discourse markers.",
            call_link="https://zoom.us/j/91234567890",
        )
        Booking.objects.create(student=maya, tutor=davit, date=next_week_start + timedelta(days=2), time="17:00", title="Pronunciation drills")
        Booking.objects.create(student=theo, tutor=davit, date=wd(1), time="12:00", title="Conversation", notes="Wants to practise small talk for a job interview.")
        Booking.objects.create(student=omar, tutor=davit, date=wd(2), time="09:00", title="Business English")
        Booking.objects.create(student=lena, tutor=davit, date=wd(3), time="18:30", title="Exam prep")

        # Past bookings
        Booking.objects.create(student=maya, tutor=davit, date=date(2026, 5, 28), time="11:00", title="Conversation practice")
        Booking.objects.create(student=theo, tutor=davit, date=date(2026, 5, 27), time="14:00", title="Conversation")
        Booking.objects.create(student=omar, tutor=davit, date=date(2026, 5, 26), time="09:00", title="Business English")
        Booking.objects.create(student=maya, tutor=davit, date=date(2026, 5, 22), time="16:00", title="IELTS speaking mock")
        Booking.objects.create(student=ines, tutor=davit, date=date(2026, 5, 21), time="10:00", title="Pronunciation drills")
        Booking.objects.create(student=omar, tutor=davit, date=date(2026, 5, 19), time="15:00", title="Business English")

        self.stdout.write("Creating availability...")

        # Availability is opt-in — a tutor starts blank and explicitly opens slots.
        # Give the demo tutor (Davit) a realistic weekday pattern across the demo
        # weeks so the seeded app/intro calendars aren't empty. Slots that collide
        # with an existing booking are simply hidden in the UI, so no need to skip.
        davit_hours = ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00", "17:00"]
        for week in (week_start, next_week_start):
            for offset in range(5):  # Mon–Fri
                day = week + timedelta(days=offset)
                for hour in davit_hours:
                    AvailabilityOverride.objects.create(
                        tutor=davit, date=day, time=hour, is_open=True
                    )

        self.stdout.write("Creating transactions and receipts...")

        # Helper to create a receipt
        def make_receipt(student, credits, date_str):
            student.receipt_seq += 1
            student.save()
            return Receipt.objects.create(
                number=f"RE-2026-{student.receipt_seq:04d}",
                student=student,
                date_str=date_str,
                credits=credits,
                unit_price_cents=site_settings.credit_price,
            )

        # SiteSettings must exist first
        site_settings = SiteSettings.objects.create(credit_price=30)

        # Note: "book" (-1) ledger entries are no longer hand-written here — every
        # Booking created above already recorded its own debit via Booking.save().
        # These remaining rows are the grants/completions that creation can't infer.

        # Maya transactions (oldest to newest so unshift order is correct)
        CreditTransaction.objects.create(
            student=maya, txn_type="buy", label="Welcome bonus", sub="May 20", amount=2,
        )
        CreditTransaction.objects.create(
            student=maya, txn_type="done", label="Session completed", sub="Davit · May 24", amount=0,
        )
        CreditTransaction.objects.create(
            student=maya, txn_type="buy", label="Purchased 5-credit pack", sub="May 28 · €145", amount=5,
        )

        # Theo transactions
        theo_receipt = make_receipt(theo, 3, "30.05.2026")
        CreditTransaction.objects.create(
            student=theo, txn_type="buy", label="Einheiten added by tutor",
            sub="May 30 · paid externally", amount=3, receipt_no=theo_receipt.number,
        )

        # Ines transactions
        CreditTransaction.objects.create(
            student=ines, txn_type="buy", label="Welcome bonus", sub="May 18", amount=1,
        )
        CreditTransaction.objects.create(
            student=ines, txn_type="done", label="Session completed", sub="Davit · May 22", amount=0,
        )

        # Omar transactions
        omar_receipt = make_receipt(omar, 10, "25.05.2026")
        CreditTransaction.objects.create(
            student=omar, txn_type="buy", label="Einheiten added by tutor",
            sub="May 25 · paid externally", amount=10, receipt_no=omar_receipt.number,
        )

        # Lena transactions
        CreditTransaction.objects.create(
            student=lena, txn_type="buy", label="Welcome bonus", sub="May 28", amount=2,
        )

        self.stdout.write("Creating student notes...")

        StudentNote.objects.create(
            tutor=davit, student=maya, text="Goal: IELTS band 7 by August. Prefers business topics.",
        )
        StudentNote.objects.create(
            tutor=davit, student=maya, text="Strong vocabulary; needs work on past-tense consistency.",
        )
        StudentNote.objects.create(
            tutor=davit, student=theo, text="Nervous speaker — build confidence with role-play.",
        )

        self.stdout.write("Creating active lessons...")

        maya_lessons = ["a1-1", "a1-2", "a1-3", "a1-4", "a2-1", "a2-2", "a2-3", "b1-1", "b1-2"]
        for lid in maya_lessons:
            ActiveLesson.objects.create(student=maya, lesson_id=lid)

        theo_lessons = ["a1-1", "a1-2", "a1-3"]
        for lid in theo_lessons:
            ActiveLesson.objects.create(student=theo, lesson_id=lid)

        omar_lessons = ["a1-1", "a1-2", "a1-3", "a1-4", "a2-1", "a2-2"]
        for lid in omar_lessons:
            ActiveLesson.objects.create(student=omar, lesson_id=lid)

        self.stdout.write(self.style.SUCCESS("Database seeded successfully!"))
        self.stdout.write("Login credentials:")
        self.stdout.write("  maya@fluent.at / password  (student)")
        self.stdout.write("  theo@fluent.at / password  (student)")
        self.stdout.write("  ines@fluent.at / password  (student)")
        self.stdout.write("  omar@fluent.at / password  (student)")
        self.stdout.write("  lena@fluent.at / password  (student)")
        self.stdout.write("  davit@fluent.at / password (tutor)")
        self.stdout.write("  admin@fluent.at / password (admin)")
