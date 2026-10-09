"""Operations: the /healthz/ endpoint and the cleanup_old_data retention job."""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest import mock

from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.db.utils import OperationalError
from django.test import TestCase, override_settings

from .management.commands.cleanup_old_data import ANONYMISED_GUEST, cleanup_old_data
from .models import AvailabilityOverride, Booking, CreditTransaction, Receipt, User

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=dt_timezone.utc)


def make_user(slug, role, **extra):
    return User.objects.create_user(
        username=slug, email=f"{slug}@fluent.at", role=role, slug=slug,
        initials=slug[:2].upper(), password="password", **extra,
    )


class HealthCheckTests(TestCase):
    @override_settings(DEBUG=False, ALLOWED_HOSTS=["thegreenpencil.at"], SECURE_SSL_REDIRECT=True)
    def test_ok_over_http_with_any_host(self):
        # Render probes over plain HTTP from inside its network.
        resp = self.client.get("/healthz/", HTTP_HOST="10.0.0.5:10000")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"ok")
        self.assertEqual(resp["Cache-Control"], "no-store")

    def test_503_when_database_is_down(self):
        with mock.patch("core.middleware.connection.cursor", side_effect=OperationalError("down")):
            resp = self.client.get("/healthz/")
        self.assertEqual(resp.status_code, 503)

    def test_other_paths_untouched(self):
        self.assertEqual(self.client.get("/robots.txt").status_code, 200)


@mock.patch("django.utils.timezone.now", return_value=NOW)
class CleanupOldDataTests(TestCase):
    def setUp(self):
        self.tutor = make_user("davit", "tutor")
        self.student = make_user("maya", "student")

    def _intro(self, day, email="guest@example.com"):
        return Booking.objects.create(
            tutor=self.tutor, student=None, date=day, time="10:00", is_intro=True,
            guest_name="Gast Name", guest_email=email, guest_phone="+43 1",
            student_name="Gast Name", student_slug="intro", notes="Hallo",
        )

    def _backdate(self, obj, when):
        type(obj).objects.filter(pk=obj.pk).update(created_at=when)

    def test_dry_run_changes_nothing(self, _now):
        old = self._intro(date(2025, 9, 1))
        report = cleanup_old_data(apply=False)
        self.assertEqual(report["intro_guests_anonymised"], 1)
        old.refresh_from_db()
        self.assertEqual(old.guest_email, "guest@example.com")

    def test_intro_guest_data_anonymised_after_a_year(self, _now):
        old = self._intro(date(2025, 9, 1))
        recent = self._intro(date(2025, 11, 1), email="new@example.com")
        cleanup_old_data(apply=True)
        old.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual((old.guest_name, old.guest_email, old.guest_phone, old.notes), ("", "", "", ""))
        self.assertEqual(old.student_name, ANONYMISED_GUEST)
        self.assertEqual(recent.guest_email, "new@example.com")
        # Already anonymised rows aren't counted again.
        self.assertEqual(cleanup_old_data(apply=True)["intro_guests_anonymised"], 0)

    def test_receipts_of_deleted_accounts_kept_for_seven_full_years(self, _now):
        # 2026 run: anything from 2018 or earlier is past the BAO period.
        expired = Receipt.objects.create(number="R-2018", student=None, date_str="1.1.2018",
                                         credits=1, unit_price_cents=40)
        kept = Receipt.objects.create(number="R-2019", student=None, date_str="1.1.2019",
                                      credits=1, unit_price_cents=40)
        own = Receipt.objects.create(number="R-own", student=self.student, date_str="1.1.2010",
                                     credits=1, unit_price_cents=40)
        self._backdate(expired, datetime(2018, 12, 31, tzinfo=dt_timezone.utc))
        self._backdate(kept, datetime(2019, 1, 2, tzinfo=dt_timezone.utc))
        self._backdate(own, datetime(2010, 1, 1, tzinfo=dt_timezone.utc))
        txn = CreditTransaction.objects.create(student=None, txn_type="buy", label="x", amount=1)
        self._backdate(txn, datetime(2017, 5, 1, tzinfo=dt_timezone.utc))
        old_lesson = Booking.objects.create(tutor=self.tutor, student=None, date=date(2018, 3, 1),
                                            time="10:00", student_name="Weg")

        report = cleanup_old_data(apply=True)

        self.assertEqual(report["expired_receipts"], 1)
        self.assertEqual(
            set(Receipt.objects.values_list("number", flat=True)), {"R-2019", "R-own"}
        )
        self.assertFalse(CreditTransaction.objects.filter(pk=txn.pk).exists())
        self.assertFalse(Booking.objects.filter(pk=old_lesson.pk).exists())

    def test_lessons_of_existing_students_never_deleted(self, _now):
        b = Booking.objects.create(tutor=self.tutor, student=self.student, date=date(2015, 1, 1), time="10:00")
        cleanup_old_data(apply=True)
        self.assertTrue(Booking.objects.filter(pk=b.pk).exists())

    def test_old_availability_and_sessions_removed(self, _now):
        AvailabilityOverride.objects.create(tutor=self.tutor, date=date(2026, 6, 1), time="10:00")
        AvailabilityOverride.objects.create(tutor=self.tutor, date=date(2026, 9, 1), time="10:00")
        Session.objects.create(session_key="a" * 32, session_data="", expire_date=NOW - timedelta(days=1))
        Session.objects.create(session_key="b" * 32, session_data="", expire_date=NOW + timedelta(days=1))
        report = cleanup_old_data(apply=True)
        self.assertEqual(report["old_availability_entries"], 1)
        self.assertEqual(report["expired_sessions"], 1)
        self.assertEqual(Session.objects.count(), 1)

    def test_inactive_students_listed_not_deleted(self, _now):
        User.objects.filter(pk=self.student.pk).update(
            date_joined=datetime(2021, 1, 1, tzinfo=dt_timezone.utc),
            last_login=datetime(2022, 1, 1, tzinfo=dt_timezone.utc),
        )
        active = make_user("theo", "student")
        User.objects.filter(pk=active.pk).update(date_joined=datetime(2021, 1, 1, tzinfo=dt_timezone.utc))
        Booking.objects.create(tutor=self.tutor, student=active, date=date(2026, 1, 1), time="10:00")
        out = StringIO()
        call_command("cleanup_old_data", "--apply", stdout=out)
        self.assertIn("inactive students", out.getvalue())
        self.assertIn("maya", out.getvalue())
        self.assertNotIn("theo", out.getvalue())
        self.assertTrue(User.objects.filter(slug="maya").exists())
