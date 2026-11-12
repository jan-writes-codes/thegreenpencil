"""Regression tests for the fixes from the pre-launch security audit."""
import json
from datetime import date

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

from .models import Booking, User
from .tests import FrozenTodayMixin, TestCase, extract_payload, make_user


@override_settings(EMAIL_ASYNC=False)
class SecurityBase(FrozenTodayMixin, TestCase):
    # FROZEN_NOW is Mon 1 Jun 2026, 10:00 in Vienna.
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.davit = make_user("davit", "tutor", first_name="Davit", last_name="Petrosyan",
                               billing_line1="Geheimgasse 7", billing_city="Wien",
                               birthday=date(1990, 5, 5))
        self.admin = make_user("admin", "admin", first_name="Studio", last_name="Admin")
        self.maya = make_user("maya", "student", first_name="Maya", last_name="Karlsson",
                              credits=5, email="maya@example.com")

    def send(self, user, url, body, method="post"):
        if user is not None:
            self.client.force_login(user)
        return getattr(self.client, method)(url, data=json.dumps(body), content_type="application/json")

    def booking(self, d, t, **extra):
        defaults = dict(student=self.maya, tutor=self.davit, date=d, time=t,
                        title="Stunde", cancel_token=f"tok-{d}-{t}")
        defaults.update(extra)
        return Booking.objects.create(**defaults)


class SeedGuardTests(TestCase):
    @override_settings(DEBUG=False, IS_TEST_SYSTEM=False)
    def test_seed_refuses_on_production(self):
        make_user("real", "student")
        with self.assertRaises(CommandError):
            call_command("seed")
        # Nothing was wiped.
        self.assertTrue(User.objects.filter(slug="real").exists())

    @override_settings(DEBUG=False, IS_TEST_SYSTEM=True)
    def test_seed_runs_on_test_system(self):
        call_command("seed", stdout=open("/dev/null", "w"))
        self.assertTrue(User.objects.filter(email="admin@fluent.at").exists())


class TutorPrivacyTests(SecurityBase):
    def test_student_payload_has_no_tutor_private_details(self):
        self.client.force_login(self.maya)
        data = extract_payload(self.client.get("/app/").content.decode())
        tutor = next(t for t in data["tutors"] if t["slug"] == "davit")
        self.assertEqual(tutor["name"], "Davit Petrosyan")
        for key in ("billing", "birthday", "credits", "usualSlots"):
            self.assertNotIn(key, tutor)
        self.assertNotIn("Geheimgasse", json.dumps(data))

    def test_admin_still_gets_full_tutor_record(self):
        self.client.force_login(self.admin)
        data = extract_payload(self.client.get("/app/").content.decode())
        tutor = next(t for t in data["tutors"] if t["slug"] == "davit")
        self.assertEqual(tutor["billing"]["line1"], "Geheimgasse 7")


class ChangePasswordTests(SecurityBase):
    URL = "/api/password/change/"

    def test_needs_current_password_outside_forced_change(self):
        r = self.send(self.maya, self.URL, {"password": "ganz-neues-pw-42", "email": "evil@example.com"})
        self.assertEqual(r.status_code, 400)
        self.maya.refresh_from_db()
        self.assertTrue(self.maya.check_password("password"))
        self.assertEqual(self.maya.email, "maya@example.com")

    def test_works_with_current_password_and_tells_old_address(self):
        r = self.send(self.maya, self.URL, {"currentPassword": "password",
                                            "password": "ganz-neues-pw-42", "email": "neu@example.com"})
        self.assertEqual(r.status_code, 200, r.content)
        self.maya.refresh_from_db()
        self.assertTrue(self.maya.check_password("ganz-neues-pw-42"))
        self.assertTrue(any("maya@example.com" in m.to for m in mail.outbox))

    def test_forced_change_needs_no_current_password(self):
        self.maya.must_change_password = True
        self.maya.save()
        r = self.send(self.maya, self.URL, {"password": "ganz-neues-pw-42"})
        self.assertEqual(r.status_code, 200, r.content)

    def test_common_password_refused(self):
        r = self.send(self.maya, self.URL, {"currentPassword": "password", "password": "password123"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("verbreitet", r.json()["error"])


class EmailChangeNoticeTests(SecurityBase):
    def test_billing_email_change_notifies_old_address(self):
        r = self.send(self.maya, "/api/users/me/billing/", {"email": "neu@example.com"}, method="put")
        self.assertEqual(r.status_code, 200, r.content)
        notice = [m for m in mail.outbox if "maya@example.com" in m.to]
        self.assertEqual(len(notice), 1)
        self.assertIn("neu@example.com", notice[0].body)

    def test_overlong_billing_fields_are_cut_not_500(self):
        r = self.send(self.maya, "/api/users/me/billing/",
                      {"name": "x" * 5000, "line1": "y" * 5000, "postcode": "1" * 500}, method="put")
        self.assertEqual(r.status_code, 200, r.content)


class StudentRescheduleTests(SecurityBase):
    def test_cannot_move_confirmed_lesson_inside_24h(self):
        b = self.booking(date(2026, 6, 1), "16:00")  # six hours away
        r = self.send(self.maya, f"/api/bookings/{b.pk}/", {"date": "2026-06-10", "time": "16:00"}, method="put")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "within_24h")
        b.refresh_from_db()
        self.assertEqual(b.date, date(2026, 6, 1))

    def test_can_move_lesson_more_than_24h_away(self):
        b = self.booking(date(2026, 6, 5), "16:00")
        r = self.send(self.maya, f"/api/bookings/{b.pk}/", {"date": "2026-06-10", "time": "16:00"}, method="put")
        self.assertEqual(r.status_code, 200, r.content)

    def test_can_still_edit_notes_inside_24h(self):
        b = self.booking(date(2026, 6, 1), "16:00")
        r = self.send(self.maya, f"/api/bookings/{b.pk}/", {"notes": "Bitte Vokabeln"}, method="put")
        self.assertEqual(r.status_code, 200, r.content)

    def test_cannot_move_into_the_past(self):
        b = self.booking(date(2026, 6, 5), "16:00")
        r = self.send(self.maya, f"/api/bookings/{b.pk}/", {"date": "2026-05-20", "time": "16:00"}, method="put")
        self.assertEqual(r.status_code, 400)

    def test_cannot_book_in_the_past(self):
        r = self.send(self.maya, "/api/bookings/", {"studentSlug": "maya", "tutorSlug": "davit",
                                                     "date": "2026-05-20", "time": "16:00"})
        self.assertEqual(r.status_code, 400)

    def test_bad_date_is_400_not_500(self):
        b = self.booking(date(2026, 6, 5), "16:00")
        r = self.send(self.davit, f"/api/bookings/{b.pk}/", {"date": "not-a-date"}, method="put")
        self.assertEqual(r.status_code, 400)


class LoginThrottleTests(SecurityBase):
    def test_one_ip_cannot_spray_many_accounts(self):
        last = None
        for i in range(55):
            last = self.send(None, "/api/login/", {"email": f"user{i}@example.com", "password": "x"})
        self.assertEqual(last.status_code, 429)


class EmailHtmlEscapingTests(TestCase):
    def test_student_name_is_escaped_in_business_mail(self):
        from . import emails
        html = emails._mail_html([f"<strong>{emails.escape('<a href=x>Klick</a>')}</strong>"])
        self.assertNotIn("<a href=x>", html)
        self.assertIn("&lt;a href=x&gt;", html)
