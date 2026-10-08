"""
Automated regression tests for the bugs fixed on this branch.

Two layers:

* Backend (Django ``Client``) tests cover everything observable server-side:
  the standalone /login/ page + auto-redirects, booking persistence and the
  role-scoped payload (a student must never receive another student's identity),
  and the ``data-role`` attribute that CSS uses to gate the nav tabs.

* Frontend (``FrontendDomTests``) tests load the *rendered* page into jsdom and
  run its real init script — the only way to catch the purely client-side
  failures: an init exception that leaves every tab visible, the header identity
  getting stuck on the static "Maya Karlsson" default, and the booking POST
  being swallowed by a render crash / shifted by a timezone bug. These require
  Node + jsdom (``cd tests/frontend && npm install``); if either is missing the
  whole class is skipped with an explanatory message rather than failing.

Map of bug -> guarding test:
  * student sees tutor/admin tabs ....... DomRoleTests.test_tabs_*
  * login is its own page w/ redirects ... LoginPageTests.*
  * bookings don't sync between users .... BookingPersistenceTests.*  +  DomBookingTests.test_booking_persists_with_local_date
  * identity always shows Maya ........... DomRoleTests.test_identity_*
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import tempfile
from datetime import date, datetime, timedelta, timezone as dt_timezone

from unittest import mock

from django.core.cache import cache
from django.test import TestCase as _DjangoTestCase, Client, override_settings
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from .models import (
    User, Booking, Receipt, CreditTransaction, ActiveLesson, LessonFile,
    SiteSettings, AvailabilityOverride,
)


# --------------------------------------------------------------------------- #
# Shared fixtures / helpers
# --------------------------------------------------------------------------- #
def make_user(slug, role, **extra):
    defaults = dict(
        username=slug,
        email=f"{slug}@fluent.at",
        role=role,
        slug=slug,
        initials=slug[:2].upper(),
    )
    defaults.update(extra)
    user = User.objects.create_user(password="password", **defaults)
    return user


def complete_payment_setup(user):
    """Give a fixture student what card payments require: a real e-mail (fixtures
    start with the <slug>@fluent.at placeholder) and a receipt address."""
    user.email = f"{user.slug}@example.com"
    user.billing_line1 = "Hauptstraße 1"
    user.billing_postcode = "1010"
    user.billing_city = "Wien"
    user.save()


def current_week_monday():
    """Monday of the week containing today (so fixtures land in the default view)."""
    today = date.today()
    return today - timedelta(days=today.weekday())


DJANGO_DATA_RE = re.compile(r'<script id="django-data" type="application/json">(.*?)</script>', re.S)
DATA_ROLE_RE = re.compile(r'<div class="app" data-role="([^"]*)"')


class TestCase(_DjangoTestCase):
    """Rate-limit counters live in the cache; every test starts with a clean one."""
    def run(self, result=None):
        cache.clear()
        return super().run(result)


WEBHOOK_SECRET = "whsec_test"


def stripe_signed(payload, secret=WEBHOOK_SECRET):
    """The header Stripe sends with ``payload``: a v1 HMAC-SHA256 signature."""
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return {"HTTP_STRIPE_SIGNATURE": f"t={ts},v1={sig}"}


def extract_payload(html):
    m = DJANGO_DATA_RE.search(html)
    assert m, "DJANGO_DATA not found in rendered page"
    return json.loads(m.group(1))


def extract_data_role(html):
    m = DATA_ROLE_RE.search(html)
    return m.group(1) if m else None


class FrozenTodayMixin:
    """Pins "now" to Mon 1 Jun 2026 — the demo week these tests' hard-coded
    dates live in — so date rules (e.g. the 30-day back-dating limit) don't
    start rejecting them as the real calendar moves on."""

    FROZEN_NOW = datetime(2026, 6, 1, 8, 0, tzinfo=dt_timezone.utc)

    def setUp(self):
        patcher = mock.patch("django.utils.timezone.now", return_value=self.FROZEN_NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()


class FluentDataMixin:
    """Creates a tutor, two students (one with a booking) and an admin."""

    def setUp(self):
        self.client = Client()
        self.davit = make_user(
            "davit", "tutor", first_name="Davit", last_name="Petrosyan", initials="DV"
        )
        self.maya = make_user(
            "maya", "student", first_name="Maya", last_name="Karlsson",
            initials="MK", credits=8,
        )
        self.ines = make_user(
            "ines", "student", first_name="Ines", last_name="Reyes",
            initials="IR", credits=3,
        )
        self.admin = make_user(
            "admin", "admin", first_name="Studio", last_name="Admin", initials="AD"
        )
        # A booking owned by *ines* so that maya's payload contains an anonymized
        # "__blocked__" entry — the exact data shape that used to crash init.
        self.ines_booking = Booking.objects.create(
            student=self.ines, tutor=self.davit,
            date=current_week_monday(), time="10:00", title="Ines session",
        )


# --------------------------------------------------------------------------- #
# Login page + auto-redirect
# --------------------------------------------------------------------------- #
class LoginPageTests(FluentDataMixin, TestCase):
    def test_landing_page_is_public_home(self):
        # The marketing landing page is the site's front door — public, at "/".
        resp = self.client.get(reverse("landing"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(reverse("landing"), "/")
        self.assertContains(resp, "The Green Pencil")
        # Booking CTAs funnel visitors into the public intro-booking calendar,
        # with sign-in as a separate entry point.
        self.assertContains(resp, 'href="/intro/"')
        self.assertContains(resp, 'href="/login/"')

    def test_landing_page_shows_pricing(self):
        # Pricing is published on the landing page, mirroring the admin packs.
        from core.models import SiteSettings
        s = SiteSettings.objects.first() or SiteSettings.objects.create()
        s.packs_json = json.dumps([
            {"n": 1, "price": "€32", "feat": False},
            {"n": 5, "price": "€145", "feat": False},
            {"n": 10, "price": "€270", "feat": True, "tag": "Popular"},
        ])
        s.save()
        resp = self.client.get(reverse("landing"))
        self.assertContains(resp, 'id="preise"')
        self.assertContains(resp, "€145")
        self.assertContains(resp, "€270")
        # Per-unit price is derived server-side (145 / 5 = 29).
        self.assertContains(resp, "€29 pro Einheit")
        # The popular pack (SiteSettings.popular_n, default 10) is badged "Beliebt".
        self.assertContains(resp, "Beliebt")

    def test_anonymous_app_redirects_to_login(self):
        resp = self.client.get(reverse("app"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], reverse("login"))

    def test_login_page_renders_standalone(self):
        resp = self.client.get(reverse("login"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="authForm"')
        self.assertContains(resp, "Anmelden")

    def test_authenticated_login_redirects_to_app(self):
        self.client.force_login(self.maya)
        resp = self.client.get(reverse("login"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], reverse("app"))

    def test_authenticated_app_renders(self):
        self.client.force_login(self.maya)
        resp = self.client.get(reverse("app"))
        self.assertEqual(resp.status_code, 200)

    def test_api_login_success_then_logout(self):
        resp = self.client.post(
            "/api/login/",
            data=json.dumps({"email": "davit@fluent.at", "password": "password"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["role"], "tutor")
        # session is live: app renders
        self.assertEqual(self.client.get(reverse("app")).status_code, 200)
        # logout, then app bounces to login again
        self.client.post("/api/logout/")
        self.assertEqual(self.client.get(reverse("app")).headers["Location"], reverse("login"))

    def test_api_login_bad_password(self):
        resp = self.client.post(
            "/api/login/",
            data=json.dumps({"email": "maya@fluent.at", "password": "nope"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    def test_api_login_unknown_email(self):
        resp = self.client.post(
            "/api/login/",
            data=json.dumps({"email": "ghost@fluent.at", "password": "password"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)


# --------------------------------------------------------------------------- #
# Legal pages + account-creation hardening  (GDPR)
# --------------------------------------------------------------------------- #
class GdprComplianceTests(FluentDataMixin, TestCase):
    def test_legal_pages_are_public(self):
        for name in ("impressum", "datenschutz"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)

    def test_new_student_gets_unique_non_default_password(self):
        # A guessable shared default ("password") on every new account is a data-
        # protection risk. Each student must get a unique, working credential.
        self.client.force_login(self.admin)
        resp = self.client.post("/api/users/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        temp = data.get("tempPassword")
        self.assertTrue(temp, "create response must surface the generated password once")
        self.assertNotEqual(temp, "password")
        created = User.objects.get(slug=data["slug"])
        self.assertFalse(created.check_password("password"))
        self.assertTrue(created.check_password(temp))


# --------------------------------------------------------------------------- #
# Credit packs: popular badge from SiteSettings.popular_n  +  photo persistence
# --------------------------------------------------------------------------- #
class CreditPackPopularTests(FluentDataMixin, TestCase):
    def _set_packs(self):
        s = SiteSettings.objects.first() or SiteSettings.objects.create()
        s.packs_json = json.dumps([
            {"n": 1, "price": "€45", "each": "€45 / session"},
            {"n": 5, "price": "€210", "each": "€42 / session"},
            {"n": 10, "price": "€400", "each": "€40 / session"},
        ])
        return s

    def test_app_popular_badge_follows_popular_n_not_packs_json(self):
        # The in-app "Einheiten aufladen" screen must derive the badge from the
        # DB value (popular_n), so it can't be stuck on a pack hand-flagged in
        # packs_json. Set popular_n to 5 and confirm only the 5-pack is featured.
        s = self._set_packs()
        s.popular_n = 5
        s.save()
        self.client.force_login(self.maya)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        packs = {p["n"]: p for p in payload["settings"]["packs"]}
        self.assertTrue(packs[5]["feat"])
        self.assertEqual(packs[5]["tag"], "Beliebt")
        self.assertFalse(packs[1]["feat"])
        self.assertFalse(packs[10]["feat"])
        self.assertEqual(payload["settings"]["popularN"], 5)

    def test_admin_can_change_popular_pack_via_settings_api(self):
        # The admin UI moves the badge by PUTting popularN — verify it persists
        # and that stale feat/tag flags sent alongside are stripped from storage.
        self._set_packs().save()
        self.client.force_login(self.admin)
        resp = self.client.put(
            "/api/settings/",
            data=json.dumps({
                "popularN": 10,
                "packs": [
                    {"n": 1, "price": "€45", "feat": True, "tag": "Beliebt"},
                    {"n": 5, "price": "€210"},
                    {"n": 10, "price": "€400"},
                ],
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        s = SiteSettings.objects.first()
        self.assertEqual(s.popular_n, 10)
        stored = json.loads(s.packs_json)
        self.assertTrue(all("feat" not in p and "tag" not in p for p in stored))
        # GET reflects the new value.
        got = self.client.get("/api/settings/").json()
        self.assertEqual(got["popularN"], 10)


class ProfilePhotoPersistenceTests(FluentDataMixin, TestCase):
    def test_admin_set_photo_persists_and_is_served_to_student(self):
        # A photo set by the admin must survive to the DB (not only client
        # state), so the student sees it when signing in on another device.
        data_url = "data:image/png;base64,iVBORw0KGgo="
        self.client.force_login(self.admin)
        resp = self.client.put(
            f"/api/users/{self.maya.slug}/",
            data=json.dumps({"photo": data_url}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.photo, data_url)
        # The student's own payload carries the photo on next load.
        self.client.force_login(self.maya)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        self.assertEqual(payload["currentUser"]["photo"], data_url)

    def test_tutor_photo_reaches_booking_calendars(self):
        # The booking calendars render tutor avatars from the tutor payload, so a
        # tutor's photo must travel both to the logged-in app and to the public
        # intro page — otherwise students only ever see initials.
        data_url = "data:image/png;base64,iVBORw0KGgo="
        self.davit.photo = data_url
        self.davit.save()
        # Logged-in app payload (student view).
        self.client.force_login(self.maya)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        davit = next(t for t in payload["tutors"] if t["slug"] == "davit")
        self.assertEqual(davit["photo"], data_url)
        # Public intro booking page payload.
        self.client.logout()
        from core.views import public_booking_payload
        pub = public_booking_payload()
        pub_davit = next(t for t in pub["tutors"] if t["slug"] == "davit")
        self.assertEqual(pub_davit["photo"], data_url)

    def test_photo_can_be_removed(self):
        self.maya.photo = "data:image/png;base64,iVBORw0KGgo="
        self.maya.save()
        self.client.force_login(self.admin)
        resp = self.client.put(
            f"/api/users/{self.maya.slug}/",
            data=json.dumps({"photo": None}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.maya.refresh_from_db()
        self.assertIsNone(self.maya.photo)


# --------------------------------------------------------------------------- #
# Booking persistence + cross-user visibility  (Bug A, backend half)
# --------------------------------------------------------------------------- #
@override_settings(EMAIL_ASYNC=False)  # no mail threads racing the test DB
class BookingPersistenceTests(FrozenTodayMixin, FluentDataMixin, TestCase):
    def _book(self, student_slug, tutor_slug, d, t):
        self.client.force_login(getattr(self, student_slug))
        return self.client.post(
            "/api/bookings/",
            data=json.dumps({
                "studentSlug": student_slug, "tutorSlug": tutor_slug,
                "date": d, "time": t, "title": "English session",
            }),
            content_type="application/json",
        )

    def test_booking_is_persisted_with_exact_date(self):
        resp = self._book("maya", "davit", "2026-06-08", "09:30")
        self.assertEqual(resp.status_code, 200)
        b = Booking.objects.get(pk=resp.json()["pk"])
        self.assertEqual(b.student, self.maya)
        self.assertEqual(b.date, date(2026, 6, 8))   # no timezone shift server-side
        self.assertEqual(b.time, "09:30")

    def test_booking_ledger_entry_names_the_tutor(self):
        # The credit ledger should say which tutor a lesson is with.
        self._book("maya", "davit", "2026-06-08", "09:30")
        txn = CreditTransaction.objects.filter(student=self.maya, txn_type="book").latest("created_at")
        self.assertIn("mit Davit", txn.sub)
        self.assertIn("09:30", txn.sub)

    def test_tutor_can_backdate_within_30_days(self):
        from django.utils import timezone
        d = (timezone.localdate() - timedelta(days=20)).isoformat()
        self.client.force_login(self.davit)  # tutor logs a forgotten session
        resp = self.client.post(
            "/api/bookings/",
            data=json.dumps({"studentSlug": "maya", "tutorSlug": "davit",
                             "date": d, "time": "15:00", "title": "English session"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Booking.objects.filter(student=self.maya, time="15:00").exists())

    def test_tutor_backdate_beyond_30_days_is_rejected(self):
        from django.utils import timezone
        d = (timezone.localdate() - timedelta(days=40)).isoformat()
        self.client.force_login(self.davit)
        resp = self.client.post(
            "/api/bookings/",
            data=json.dumps({"studentSlug": "maya", "tutorSlug": "davit",
                             "date": d, "time": "15:00", "title": "English session"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "too_far_back")
        self.assertFalse(Booking.objects.filter(student=self.maya, time="15:00").exists())

    def test_cancellation_refund_entry_names_the_tutor(self):
        # The refund entry (shown under "Stunden") must also name the tutor.
        pk = self._book("maya", "davit", "2026-06-08", "09:30").json()["pk"]
        self.client.force_login(self.davit)  # tutor cancels → credit refunded
        self.client.delete(f"/api/bookings/{pk}/")
        ref = CreditTransaction.objects.filter(
            student=self.maya, label__icontains="storniert"
        ).latest("created_at")
        self.assertIn("mit Davit", ref.sub)

    def test_tutor_sees_students_booking(self):
        self._book("maya", "davit", "2026-06-08", "09:30")
        self.client.force_login(self.davit)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        match = [
            b for b in payload["bookings"]
            if b["studentId"] == "maya" and b["date"] == "2026-06-08" and b["time"] == "09:30"
        ]
        self.assertEqual(len(match), 1, "tutor should see the student's booking by identity")

    def test_other_student_sees_booking_anonymized(self):
        self._book("maya", "davit", "2026-06-08", "09:30")
        self.client.force_login(self.ines)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        # The slot is blocked for ines...
        blocked = [
            b for b in payload["bookings"]
            if b["date"] == "2026-06-08" and b["time"] == "09:30"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["studentId"], "__blocked__")
        self.assertEqual(blocked[0]["title"], "")          # no leaked details
        # ...and maya's real identity is never sent to ines.
        self.assertFalse(any(b["studentId"] == "maya" for b in payload["bookings"]))

    def test_tutor_assigns_student_persists(self):
        # Davit books Ines -> Ines must see it on next load.
        self.client.force_login(self.davit)
        resp = self.client.post(
            "/api/bookings/",
            data=json.dumps({
                "studentSlug": "ines", "tutorSlug": "davit",
                "date": "2026-06-09", "time": "14:30", "title": "English session",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.client.force_login(self.ines)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        match = [
            b for b in payload["bookings"]
            if b["studentId"] == "ines" and b["date"] == "2026-06-09" and b["time"] == "14:30"
        ]
        self.assertEqual(len(match), 1)


# --------------------------------------------------------------------------- #
# Role-scoped payload + data-role attribute
# --------------------------------------------------------------------------- #
class RoleScopingTests(FluentDataMixin, TestCase):
    def _payload_for(self, user):
        self.client.force_login(user)
        html = self.client.get(reverse("app")).content.decode()
        return extract_payload(html), extract_data_role(html)

    def test_student_payload_is_self_only(self):
        payload, data_role = self._payload_for(self.maya)
        self.assertEqual(data_role, "student")
        self.assertEqual(payload["role"], "student")
        self.assertEqual(payload["currentUserId"], "maya")
        slugs = {s["slug"] for s in payload["students"]}
        self.assertEqual(slugs, {"maya"}, "a student must not receive other students")

    def test_tutor_payload_has_all_students(self):
        payload, data_role = self._payload_for(self.davit)
        self.assertEqual(data_role, "tutor")
        slugs = {s["slug"] for s in payload["students"]}
        self.assertEqual(slugs, {"maya", "ines"})

    def test_admin_data_role(self):
        _payload, data_role = self._payload_for(self.admin)
        self.assertEqual(data_role, "admin")


# --------------------------------------------------------------------------- #
# Admin user management: create/edit must persist + be loggable into
# --------------------------------------------------------------------------- #
class AdminUserManagementTests(FluentDataMixin, TestCase):
    def test_created_and_edited_student_can_log_in(self):
        self.client.force_login(self.admin)
        # 1) create a blank student
        created = self.client.post("/api/users/", data="{}", content_type="application/json").json()
        slug = created["slug"]
        self.assertTrue(User.objects.filter(slug=slug).exists())
        # 2) edit name + login email + password (what the admin editor sends)
        resp = self.client.put(
            f"/api/users/{slug}/",
            data=json.dumps({
                "name": "Jan Heissenberger",
                "email": "jan@fluent.at",
                "password": "geheim123",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        # persisted to the DB
        u = User.objects.get(slug=slug)
        self.assertEqual(u.email, "jan@fluent.at")
        # initials recomputed from the new name -> avatar is correct after reload
        self.assertEqual(u.initials, "JH")
        self.assertEqual(resp.json()["initials"], "JH")
        # 3) the new credentials actually work (the reported bug)
        fresh = Client()
        login = fresh.post(
            "/api/login/",
            data=json.dumps({"email": "jan@fluent.at", "password": "geheim123"}),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["role"], "student")
        # The admin-set password is a handed-over secret: the first login must
        # replace it with an own one before the app opens.
        self.assertTrue(login.json()["mustChangePassword"])
        self.assertEqual(fresh.get(reverse("app")).headers["Location"], reverse("login"))
        resp = fresh.post(
            "/api/password/change/",
            data=json.dumps({"password": "mein-eigenes-1"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(fresh.get(reverse("app")).status_code, 200)

    def test_billing_name_change_updates_initials(self):
        self.client.force_login(self.maya)
        self.client.put(
            "/api/users/me/billing/",
            data=json.dumps({"name": "Maya Olsen"}),
            content_type="application/json",
        )
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.initials, "MO")

    def test_admin_cannot_reset_credits_directly(self):
        # The old fraud vector: a free-form credit reset. The PUT must ignore it.
        self.client.force_login(self.admin)
        before = self.maya.credits
        resp = self.client.put(
            "/api/users/maya/",
            data=json.dumps({"credits": 999}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, before, "credits must not be settable via the user editor")

    def test_admin_sets_opening_balance_as_receiptless_transaction(self):
        self.client.force_login(self.admin)
        before = self.maya.credits
        resp = self.client.post(
            "/api/students/maya/opening-credit/",
            data=json.dumps({"n": 8}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, before + 8)
        txn = CreditTransaction.objects.get(student=self.maya, txn_type="open")
        self.assertEqual(txn.amount, 8)
        self.assertEqual(txn.receipt_no, "", "an opening balance carries no receipt")
        self.assertFalse(Receipt.objects.filter(student=self.maya).exists())

    def test_opening_balance_is_one_time(self):
        self.client.force_login(self.admin)
        first = self.client.post(
            "/api/students/maya/opening-credit/",
            data=json.dumps({"n": 5}), content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            "/api/students/maya/opening-credit/",
            data=json.dumps({"n": 5}), content_type="application/json",
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error"], "already_set")
        self.assertEqual(CreditTransaction.objects.filter(student=self.maya, txn_type="open").count(), 1)

    def test_opening_balance_can_be_reversed_and_rebooked(self):
        self.client.force_login(self.admin)
        before = self.maya.credits
        self.client.post("/api/students/maya/opening-credit/",
                         data=json.dumps({"n": 8}), content_type="application/json")
        txn = CreditTransaction.objects.get(student=self.maya, txn_type="open", amount=8)
        # Reverse it (fat-finger correction).
        resp = self.client.post(f"/api/transactions/{txn.pk}/cancel/")
        self.assertEqual(resp.status_code, 200)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, before, "reversal must undo the credits")
        txn.refresh_from_db()
        self.assertTrue(txn.cancelled)
        self.assertTrue(CreditTransaction.objects.filter(
            student=self.maya, txn_type="open", amount=-8, reverses=txn).exists())
        # No receipt/Storno-receipt is ever created for an opening balance.
        self.assertFalse(Receipt.objects.filter(student=self.maya).exists())
        # A fresh opening balance can now be booked again.
        again = self.client.post("/api/students/maya/opening-credit/",
                                 data=json.dumps({"n": 3}), content_type="application/json")
        self.assertEqual(again.status_code, 200)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, before + 3)

    def test_only_admin_sets_opening_balance(self):
        self.client.force_login(self.davit)  # tutor
        resp = self.client.post(
            "/api/students/maya/opening-credit/",
            data=json.dumps({"n": 5}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_admin_saves_student_billing_address(self):
        # The path used when an admin edits a student's address (incl. while
        # "viewing as" that student): it must persist to the *student*, so the
        # student sees it on next login.
        self.client.force_login(self.admin)
        resp = self.client.put(
            "/api/users/maya/",
            data=json.dumps({"name": "Maya Karlsson", "billing": {
                "line1": "Hauptstraße 5", "postcode": "1020", "city": "Wien",
                "country": "Österreich",
            }}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.billing_line1, "Hauptstraße 5")
        self.assertEqual(self.maya.billing_postcode, "1020")
        self.assertEqual(self.maya.billing_city, "Wien")
        # And the student receives it in their own payload on login.
        self.client.force_login(self.maya)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        self.assertEqual(payload["currentUser"]["billing"]["line1"], "Hauptstraße 5")


# --------------------------------------------------------------------------- #
# Multi-tutor: admin can create/manage several tutors; their calendars are
# isolated from one another.
# --------------------------------------------------------------------------- #
class MultiTutorTests(FluentDataMixin, TestCase):
    def test_admin_creates_tutor_who_can_log_in(self):
        self.client.force_login(self.admin)
        created = self.client.post(
            "/api/users/",
            data=json.dumps({"role": "tutor", "name": "Lena Bauer"}),
            content_type="application/json",
        ).json()
        self.assertTrue(created["slug"].startswith("tut"))
        self.assertEqual(created["role"], "tutor")
        self.assertEqual(created["initials"], "LB")
        self.assertIn("tempPassword", created)
        # the temp password actually logs in, as a tutor
        fresh = Client()
        login = fresh.post(
            "/api/login/",
            data=json.dumps({"email": created["email"], "password": created["tempPassword"]}),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["role"], "tutor")

    def test_edited_tutor_credentials_work(self):
        """The reported bug: change a tutor's login, then sign in with it."""
        self.client.force_login(self.admin)
        resp = self.client.put(
            f"/api/users/{self.davit.slug}/",
            data=json.dumps({
                "name": "Davit Petrosyan",
                "email": "new.davit@fluent.at",
                "password": "totallynew99",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        fresh = Client()
        login = fresh.post(
            "/api/login/",
            data=json.dumps({"email": "new.davit@fluent.at", "password": "totallynew99"}),
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["role"], "tutor")

    def test_invalid_role_is_rejected(self):
        self.client.force_login(self.admin)
        resp = self.client.post(
            "/api/users/",
            data=json.dumps({"role": "admin"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_availability_is_isolated_per_tutor(self):
        other = make_user("lena", "tutor", first_name="Lena", last_name="Bauer", initials="LB")
        self.client.force_login(self.admin)
        # admin opens the SAME slot for davit but closes it for lena
        for slug, is_open in ((self.davit.slug, True), (other.slug, False)):
            r = self.client.post(
                "/api/availability/",
                data=json.dumps({"date": "2026-06-02", "time": "11:00", "isOpen": is_open, "tutorSlug": slug}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 200)
        # the payload keys each tutor's overrides separately — no collision
        html = self.client.get(reverse("app")).content.decode()
        avail = extract_payload(html)["availability"]
        self.assertEqual(avail[self.davit.slug]["2026-06-02|11:00"], True)
        self.assertEqual(avail[other.slug]["2026-06-02|11:00"], False)

    def test_admin_custom_time_requires_known_tutor(self):
        self.client.force_login(self.admin)
        r = self.client.post(
            "/api/custom-times/",
            data=json.dumps({"date": "2026-06-02", "time": "07:30", "tutorSlug": "nope"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)


# --------------------------------------------------------------------------- #
# Authorization (RBAC + object-level / IDOR) and login hardening
# --------------------------------------------------------------------------- #
class AuthorizationTests(FluentDataMixin, TestCase):
    def _post(self, url, body=None):
        return self.client.post(url, data=json.dumps(body or {}), content_type="application/json")

    def _put(self, url, body=None):
        return self.client.put(url, data=json.dumps(body or {}), content_type="application/json")

    # ---- anonymous is locked out of every mutating endpoint (401) ----
    def test_anonymous_endpoints_require_auth(self):
        for method, url in [
            ("post", "/api/users/"),
            ("put", "/api/users/maya/"),
            ("post", "/api/credits/maya/"),
            ("post", "/api/availability/"),
            ("post", "/api/custom-times/"),
            ("post", "/api/notes/maya/"),
            ("post", "/api/lessons/maya/"),
            ("put", "/api/settings/"),
            ("post", "/api/bookings/"),
        ]:
            fn = getattr(self, f"_{method}")
            self.assertEqual(fn(url).status_code, 401, f"{method} {url} should be 401 for anonymous")

    # ---- a student is forbidden from privileged endpoints (403) ----
    def test_student_cannot_reach_admin_or_tutor_endpoints(self):
        self.client.force_login(self.maya)
        self.assertEqual(self.client.get("/api/users/").status_code, 403)
        self.assertEqual(self._post("/api/users/").status_code, 403)
        self.assertEqual(self._put("/api/users/ines/", {"name": "Hacked"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/users/ines/").status_code, 403)
        self.assertEqual(self._post("/api/credits/maya/", {"n": 99}).status_code, 403)
        self.assertEqual(self._post("/api/availability/", {"date": "2026-06-01", "time": "10:00"}).status_code, 403)
        self.assertEqual(self._post("/api/custom-times/", {"date": "2026-06-01", "time": "10:00"}).status_code, 403)
        self.assertEqual(self._post("/api/notes/ines/", {"text": "x"}).status_code, 403)
        self.assertEqual(self._post("/api/lessons/ines/", {"lessonId": "a1-1"}).status_code, 403)
        self.assertEqual(self._put("/api/settings/", {"creditPrice": 1}).status_code, 403)

    def test_student_cannot_grant_self_credits(self):
        self.client.force_login(self.maya)
        before = self.maya.credits
        self.assertEqual(self._post("/api/credits/maya/", {"n": 100}).status_code, 403)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, before)

    def test_student_cannot_book_as_another_student(self):
        self.client.force_login(self.maya)
        resp = self._post("/api/bookings/", {
            "studentSlug": "ines", "tutorSlug": "davit", "date": "2026-06-08", "time": "09:30",
        })
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Booking.objects.filter(student=self.ines, date=date(2026, 6, 8), time="09:30").exists())

    def test_student_cannot_modify_others_booking_idor(self):
        # self.ines_booking belongs to Ines; Maya must not touch it.
        self.client.force_login(self.maya)
        self.assertEqual(self._put(f"/api/bookings/{self.ines_booking.pk}/", {"time": "08:00"}).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/bookings/{self.ines_booking.pk}/").status_code, 403)
        self.assertTrue(Booking.objects.filter(pk=self.ines_booking.pk).exists())

    def test_student_can_manage_own_booking(self):
        self.client.force_login(self.maya)
        mine = Booking.objects.create(student=self.maya, tutor=self.davit, date=date(2026, 6, 8), time="09:30")
        self.assertEqual(self._put(f"/api/bookings/{mine.pk}/", {"time": "10:30", "notes": "hi"}).status_code, 200)
        mine.refresh_from_db()
        self.assertEqual(mine.time, "10:30")
        # tutor-only fields are ignored for students
        self._put(f"/api/bookings/{mine.pk}/", {"tutorNotes": "secret", "callLink": "http://x"})
        mine.refresh_from_db()
        self.assertEqual(mine.tutor_notes, "")
        self.assertEqual(mine.call_link, "")
        self.assertEqual(self.client.delete(f"/api/bookings/{mine.pk}/").status_code, 200)

    # ---- a tutor can run tutor endpoints but not admin ones ----
    def test_tutor_permissions(self):
        self.client.force_login(self.davit)
        self.assertEqual(self._post("/api/credits/maya/", {"n": 1}).status_code, 200)
        self.assertEqual(self._post("/api/notes/maya/", {"text": "great progress"}).status_code, 200)
        self.assertEqual(self._post("/api/availability/", {"date": "2026-06-01", "time": "10:00", "isOpen": False}).status_code, 200)
        # but not admin-only user management / settings
        self.assertEqual(self.client.get("/api/users/").status_code, 403)
        self.assertEqual(self._put("/api/settings/", {"creditPrice": 40}).status_code, 403)

    # ---- admin guards ----
    def test_admin_cannot_delete_self(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.delete("/api/users/admin/").status_code, 400)
        self.assertTrue(User.objects.filter(slug="admin").exists())

    def test_admin_cannot_set_duplicate_email(self):
        self.client.force_login(self.admin)
        resp = self._put("/api/users/ines/", {"email": "maya@fluent.at"})
        self.assertEqual(resp.status_code, 400)
        self.ines.refresh_from_db()
        self.assertNotEqual(self.ines.email, "maya@fluent.at")

    # ---- login doesn't leak which emails exist ----
    def test_login_errors_do_not_enumerate(self):
        unknown = self.client.post(
            "/api/login/",
            data=json.dumps({"email": "ghost@fluent.at", "password": "x"}),
            content_type="application/json",
        )
        wrong = self.client.post(
            "/api/login/",
            data=json.dumps({"email": "maya@fluent.at", "password": "wrong"}),
            content_type="application/json",
        )
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(wrong.status_code, 400)
        # identical message -> can't tell a real account from a fake one
        self.assertEqual(unknown.json()["error"], wrong.json()["error"])


class CsrfTests(FluentDataMixin, TestCase):
    def test_mutating_endpoints_enforce_csrf(self):
        # A logged-in session without a CSRF token must still be rejected.
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.admin)
        resp = c.post("/api/users/", data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 403)


# --------------------------------------------------------------------------- #
# Pricing: pack purchases bill the package total, not credits × per-credit rate
# --------------------------------------------------------------------------- #
class PricingReceiptTests(FluentDataMixin, TestCase):
    def _add_credits(self, student_slug, n):
        self.client.force_login(self.davit)  # tutor grants credits
        return self.client.post(
            f"/api/credits/{student_slug}/",
            data=json.dumps({"n": n}),
            content_type="application/json",
        )

    def test_pack_purchase_bills_package_total(self):
        # Default packs: 10 credits = €270 (i.e. €27/credit, not 10 × €30).
        self._add_credits("maya", 10)
        r = Receipt.objects.filter(student=self.maya, credits=10).latest("created_at")
        self.assertEqual(r.unit_price_cents, 27)
        self.assertEqual(r.credits * r.unit_price_cents, 270)  # total shown on receipt

    def test_five_pack_uses_pack_price(self):
        self._add_credits("maya", 5)
        r = Receipt.objects.filter(student=self.maya, credits=5).latest("created_at")
        self.assertEqual(r.credits * r.unit_price_cents, 145)

    def test_non_pack_amount_uses_per_credit_rate(self):
        # 3 credits isn't a pack -> falls back to the €30 per-credit receipts rate.
        self._add_credits("maya", 3)
        r = Receipt.objects.filter(student=self.maya, credits=3).latest("created_at")
        self.assertEqual(r.unit_price_cents, 30)
        self.assertEqual(r.credits * r.unit_price_cents, 90)


# --------------------------------------------------------------------------- #
# Lesson PDF materials: upload (tutor), download (access-controlled), scoping
# --------------------------------------------------------------------------- #
_LESSON_MEDIA = tempfile.mkdtemp(prefix="fluent-test-media-")


@override_settings(MEDIA_ROOT=_LESSON_MEDIA)
class LessonFileTests(FluentDataMixin, TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_LESSON_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        ActiveLesson.objects.create(student=self.maya, lesson_id="a1-1")  # ines does NOT have it

    def _pdf(self, name="worksheet.pdf"):
        return SimpleUploadedFile(name, b"%PDF-1.4 test pdf", content_type="application/pdf")

    def _upload(self, lesson="a1-1"):
        return self.client.post(f"/api/lesson-files/{lesson}/", {"file": self._pdf()})

    def test_tutor_uploads_pdf(self):
        self.client.force_login(self.davit)
        resp = self._upload()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "worksheet.pdf")
        self.assertEqual(LessonFile.objects.filter(lesson_id="a1-1").count(), 1)

    def test_disallowed_type_rejected(self):
        self.client.force_login(self.davit)
        bad = SimpleUploadedFile("malware.exe", b"MZ", content_type="application/octet-stream")
        resp = self.client.post("/api/lesson-files/a1-1/", {"file": bad})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(LessonFile.objects.count(), 0)

    def test_non_pdf_materials_allowed_with_kind(self):
        self.client.force_login(self.davit)
        mp3 = SimpleUploadedFile("listening.mp3", b"ID3 audio", content_type="audio/mpeg")
        resp = self.client.post("/api/lesson-files/a1-1/", {"file": mp3})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["kind"], "audio")
        self.assertEqual(resp.json()["ext"], "MP3")
        # served with the right content-type
        self.client.force_login(self.maya)
        dl = self.client.get(resp.json()["url"])
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl["Content-Type"], "audio/mpeg")

    def test_oversize_rejected(self):
        self.client.force_login(self.davit)
        with mock.patch("core.views.MAX_LESSON_FILE_BYTES", 4):
            resp = self._upload()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(LessonFile.objects.count(), 0)

    def test_student_cannot_upload_or_delete(self):
        self.client.force_login(self.davit)
        lf_id = self._upload().json()["id"]
        self.client.force_login(self.maya)
        self.assertEqual(self._upload().status_code, 403)
        self.assertEqual(self.client.delete(f"/api/lesson-files/{lf_id}/").status_code, 403)
        self.assertTrue(LessonFile.objects.filter(pk=lf_id).exists())

    def test_download_access_control(self):
        self.client.force_login(self.davit)
        lf_id = self._upload().json()["id"]
        url = f"/api/lesson-files/download/{lf_id}/"
        # maya has a1-1 unlocked -> can download
        self.client.force_login(self.maya)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertIn("attachment", r["Content-Disposition"])
        # ines has NOT unlocked a1-1 -> forbidden
        self.client.force_login(self.ines)
        self.assertEqual(self.client.get(url).status_code, 403)
        # anonymous -> 401
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_payload_scopes_files_to_unlocked_lessons(self):
        self.client.force_login(self.davit)
        self._upload()
        # maya (a1-1 unlocked) receives the file
        self.client.force_login(self.maya)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        self.assertIn("a1-1", payload["lessonFiles"])
        self.assertEqual(payload["lessonFiles"]["a1-1"][0]["name"], "worksheet.pdf")
        # ines (locked) does not
        self.client.force_login(self.ines)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        self.assertNotIn("a1-1", payload.get("lessonFiles", {}))

    def test_tutor_deletes_file(self):
        self.client.force_login(self.davit)
        lf_id = self._upload().json()["id"]
        self.assertEqual(self.client.delete(f"/api/lesson-files/{lf_id}/").status_code, 200)
        self.assertFalse(LessonFile.objects.filter(pk=lf_id).exists())


# --------------------------------------------------------------------------- #
# Frontend (jsdom) tests — run the real init script in a headless DOM
# --------------------------------------------------------------------------- #
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "tests", "frontend")
PROBE = os.path.join(FRONTEND_DIR, "dom_probe.js")
INTRO_PROBE = os.path.join(FRONTEND_DIR, "intro_probe.js")


def _node_bin():
    return shutil.which("node")


def _jsdom_installed():
    return os.path.isdir(os.path.join(FRONTEND_DIR, "node_modules", "jsdom"))


class _DomProbeBase(FluentDataMixin, TestCase):
    """Renders a page for a user and runs the jsdom probe against it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._node = _node_bin()
        cls._has_jsdom = _jsdom_installed()

    def _skip_if_unavailable(self):
        if not self._node:
            self.skipTest("node not found on PATH — skipping jsdom frontend tests")
        if not self._has_jsdom:
            self.skipTest(
                "jsdom not installed — run `cd tests/frontend && npm install` "
                "to enable frontend tests"
            )

    def run_probe(self, user, book=False, tz=None, admin_rename=False, admin_save=False, admin_pricing=False, learning=False, preview=False, admin_add_tutor=False, buy=False, quick_adds=False):
        self._skip_if_unavailable()
        self.client.force_login(user)
        html = self.client.get(reverse("app")).content.decode()
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
            fh.write(html)
            path = fh.name
        try:
            env = dict(os.environ)
            if tz:
                env["TZ"] = tz
            cmd = [self._node, PROBE, path]
            if book:
                cmd.append("--book")
            if admin_rename:
                cmd.append("--admin-rename")
            if admin_save:
                cmd.append("--admin-save")
            if admin_add_tutor:
                cmd.append("--admin-add-tutor")
            if admin_pricing:
                cmd.append("--admin-pricing")
            if learning:
                cmd.append("--learning")
            if preview:
                cmd.append("--preview")
            if buy:
                cmd.append("--buy")
            if quick_adds:
                cmd.append("--quick-adds")
            out = subprocess.run(
                cmd, capture_output=True, text=True, env=env, timeout=60
            )
            self.assertEqual(
                out.returncode, 0,
                msg=f"probe failed: {out.stderr or out.stdout}",
            )
            return json.loads(out.stdout)
        finally:
            os.unlink(path)


class DomRoleTests(_DomProbeBase):
    EXPECTED = {
        "maya":  ("Maya Karlsson", {"book", "account", "games", "files"}),
        "davit": ("Davit Petrosyan", {"teacher", "students"}),
        "admin": ("Studio Admin", {"admin"}),
    }

    def _check(self, user, expected_name, expected_tabs):
        r = self.run_probe(user)
        self.assertEqual(r["initErrors"], [], "init must not throw")
        # identity (Bug B)
        self.assertEqual(r["identity"]["name"], expected_name)
        self.assertEqual(r["identity"]["mail"], f"{user.slug}@fluent.at")
        # tab gating (the "student sees everything" bug)
        visible = {v for v, disp in r["tabs"].items() if disp != "none"}
        self.assertEqual(visible, expected_tabs)

    def test_identity_and_tabs_student(self):
        self._check(self.maya, *self.EXPECTED["maya"])

    def test_identity_and_tabs_tutor(self):
        self._check(self.davit, *self.EXPECTED["davit"])

    def test_identity_and_tabs_admin(self):
        self._check(self.admin, *self.EXPECTED["admin"])


class XssTests(_DomProbeBase):
    """User-supplied text (guest/student/tutor names, booking titles) must render
    as text, never as markup, in every role's app — and must not be able to
    close the <script> block that carries the page data."""
    MARK = "<i/data-xss>Eve"            # no spaces: survives name.split(" ")[0]
    BREAKOUT = "</script><script>window.__pwned=1</script>"

    def setUp(self):
        super().setUp()
        today = date.today()
        self.maya.first_name = self.MARK
        self.maya.save()
        self.davit.first_name = self.MARK
        self.davit.save()
        Booking.objects.create(student=self.maya, tutor=self.davit, date=today,
                               time="19:00", title=self.MARK)
        Booking.objects.create(tutor=self.davit, date=today + timedelta(days=1), time="19:00",
                               is_intro=True, guest_name=self.MARK, guest_email="eve@example.at",
                               student_name=self.MARK, student_slug="intro")

    def test_names_and_titles_render_as_text(self):
        for user in (self.davit, self.admin, self.maya):
            r = self.run_probe(user)
            self.assertEqual(r["initErrors"], [], f"init must not throw ({user.slug})")
            self.assertEqual(r["xss"], 0, f"markup injected into {user.slug}'s app")

    def test_page_data_cannot_close_its_script_block(self):
        self.client.force_login(self.maya)
        self.client.put("/api/users/me/billing/", data=json.dumps({"name": self.BREAKOUT}),
                        content_type="application/json")
        self.client.force_login(self.davit)
        html = self.client.get(reverse("app")).content.decode()
        self.assertNotIn(self.BREAKOUT, html)
        maya = next(s for s in extract_payload(html)["students"] if s["slug"] == "maya")
        self.assertEqual(maya["name"], self.BREAKOUT)   # data survives intact
        self.davit.first_name = self.BREAKOUT
        self.davit.save()
        self.assertNotIn(self.BREAKOUT, self.client.get("/intro/").content.decode())

    def test_photo_must_be_an_image_data_url(self):
        self.client.force_login(self.admin)
        for bad in ("javascript:alert(1)", "data:text/html;base64,PHNjcmlwdD4=", "x');}*{background:red"):
            r = self.client.put(f"/api/users/{self.maya.slug}/", data=json.dumps({"photo": bad}),
                                content_type="application/json")
            self.assertEqual(r.status_code, 400, bad)


class DomAdminTests(_DomProbeBase):
    def test_avatar_initials_update_live_on_rename(self):
        # Admin editor opens on a user; typing a new name must update the
        # avatar initials immediately (no save / reload).
        r = self.run_probe(self.admin, admin_rename=True)
        self.assertEqual(r["initErrors"], [], "init must not throw")
        self.assertEqual(r["adminRename"]["avatarInitials"], "JH")
        self.assertEqual(r["adminRename"]["headingName"], "Jan Heissenberger")

    def test_admin_add_and_save_persist_to_server(self):
        # The reported bug: editing a student in the admin UI never hit the
        # server, so the account couldn't be logged into. Drive the real UI and
        # assert both the create (POST) and the edit (PUT) are sent.
        r = self.run_probe(self.admin, admin_save=True)
        self.assertEqual(r["initErrors"], [], "init must not throw")
        s = r["adminSave"]
        self.assertTrue(s["createPosted"], "adding a student must POST /api/users/")
        self.assertIsNotNone(s["editPut"], "saving must PUT /api/users/<slug>/")
        self.assertEqual(s["editPut"]["body"]["email"], "jan@fluent.at")
        self.assertEqual(s["editPut"]["body"]["name"], "Jan Heissenberger")
        self.assertEqual(s["editPut"]["body"]["password"], "geheim123")
        # A newly added student must open in the *student* editor — not the tutor
        # mask (the bug: the locally-mirrored account had no role, so it rendered
        # as a tutor with no credits/billing fields).
        self.assertEqual(s["editorRole"], "Schüler")
        self.assertTrue(s["hasOpeningField"], "student editor must show the opening-balance control")
        self.assertTrue(s["hasRemoveStudent"], "student editor must offer 'Schüler entfernen'")

    def test_admin_add_tutor_button_creates_and_selects_tutor(self):
        # The new "+ Tutor hinzufügen" button: POST must carry role:tutor, and the
        # created tutor must become the selected, editable account (temp password
        # shown so the admin can hand it over) with a "remove tutor" control.
        r = self.run_probe(self.admin, admin_add_tutor=True)
        self.assertEqual(r["initErrors"], [], "init must not throw")
        a = r["adminAddTutor"]
        self.assertEqual(a["postedRole"], "tutor", "must POST role:tutor")
        self.assertEqual(a["editorPassword"], "tmp-tutor-pw", "temp password shown in editor")
        self.assertTrue(a["hasRemoveTutor"], "tutor editor must offer removal")


class DomPricingTests(_DomProbeBase):
    def test_per_session_is_readonly_and_auto_derived(self):
        r = self.run_probe(self.admin, admin_pricing=True)
        self.assertEqual(r["initErrors"], [])
        p = r["adminPricing"]
        self.assertTrue(p["readonly"], "per-session field must not be editable")
        # 1-credit pack priced at €100 -> €100 / session
        self.assertEqual(p["eachAfterPrice"], "€100 / session")
        # credits = 0 -> blank, never NaN/Infinity (ZeroDivision -> none)
        self.assertEqual(p["eachAfterZeroCredits"], "")


@override_settings(MEDIA_ROOT=_LESSON_MEDIA)
class DomLessonTests(_DomProbeBase):
    def test_student_sees_real_download_link(self):
        ActiveLesson.objects.create(student=self.maya, lesson_id="a1-1")
        lf = LessonFile.objects.create(
            lesson_id="a1-1",
            file=SimpleUploadedFile("worksheet.pdf", b"%PDF-1.4", content_type="application/pdf"),
            original_name="worksheet.pdf", uploaded_by=self.davit,
        )
        r = self.run_probe(self.maya, learning=True)
        self.assertEqual(r["initErrors"], [])
        self.assertIn(f"/api/lesson-files/download/{lf.pk}/", r["learning"]["fileLinks"])

    def test_tutor_preview_shows_student_materials(self):
        ActiveLesson.objects.create(student=self.maya, lesson_id="a1-1")
        lf = LessonFile.objects.create(
            lesson_id="a1-1",
            file=SimpleUploadedFile("worksheet.pdf", b"%PDF-1.4", content_type="application/pdf"),
            original_name="worksheet.pdf", uploaded_by=self.davit,
        )
        r = self.run_probe(self.davit, preview=True)
        self.assertEqual(r["initErrors"], [])
        self.assertIn(f"/api/lesson-files/download/{lf.pk}/", r["preview"]["fileLinks"])


class DomQuickAddTests(_DomProbeBase):
    def test_quick_add_buttons_match_admin_packs(self):
        # Custom packs -> the tutor's +N credit buttons must follow them.
        SiteSettings.objects.all().delete()
        SiteSettings.objects.create(credit_price=30, packs_json=json.dumps([
            {"n": 2, "price": "€60"}, {"n": 4, "price": "€110"}, {"n": 8, "price": "€200"},
        ]))
        r = self.run_probe(self.davit, quick_adds=True)
        self.assertEqual(r["initErrors"], [])
        self.assertEqual(sorted(set(r["quickAdds"])), [2, 4, 8])


class DomBookingTests(_DomProbeBase):
    def setUp(self):
        super().setUp()
        # Availability is opt-in now (no seeded defaults). The calendar runs on
        # the real current date, so open a few of Davit's slots over the coming
        # days (2+ days out, clear of the 24h window) for the flow to click.
        from core.models import AvailabilityOverride
        for offset in range(2, 10):
            for time in ("09:00", "10:00", "11:00", "14:00"):
                AvailabilityOverride.objects.create(
                    tutor=self.davit, date=date.today() + timedelta(days=offset),
                    time=time, is_open=True,
                )

    def test_booking_persists_with_local_date(self):
        # Run under a large positive UTC offset: the old toISOString() code
        # shifted a local-midnight date back a day here.
        r = self.run_probe(self.maya, book=True, tz="Asia/Tokyo")
        self.assertEqual(r["initErrors"], [], "init must not throw")
        b = r["booking"]
        self.assertTrue(b.get("posted"), f"booking POST was not sent: {b}")
        self.assertRegex(b["postedDate"], r"^\d{4}-\d{2}-\d{2}$")
        # The POSTed day must match the day the UI showed the user (no tz shift).
        self.assertIsNotNone(b["panelDay"])
        self.assertEqual(
            b["postedDay"], b["panelDay"],
            msg=f"booking date shifted: sent {b['postedDate']} but UI showed {b['panelDate']}",
        )


# --------------------------------------------------------------------------- #
# Immutable history + GDPR "anonymise & keep" on account deletion
# --------------------------------------------------------------------------- #
class HistoryRetentionTests(FluentDataMixin, TestCase):
    """Purchase/lesson history must never be altered when an account is deleted:
    the financial records survive (statutory retention) while the personal account
    is erased (GDPR). The records carry frozen identity/billing snapshots so they
    stay readable verbatim after the FK is detached."""

    def _grant(self, n=5):
        # Tutor grants credits -> creates a Receipt + CreditTransaction.
        self.client.force_login(self.davit)
        resp = self.client.post(
            f"/api/credits/{self.maya.slug}/",
            data=json.dumps({"n": n}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_deleting_student_keeps_receipt_with_frozen_snapshot(self):
        self.maya.billing_name = "Maya Karlsson"
        self.maya.billing_line1 = "Hauptstraße 1"
        self.maya.billing_postcode = "1010"
        self.maya.billing_city = "Wien"
        self.maya.save()
        self._grant(5)
        receipt = Receipt.objects.get(student=self.maya)
        # Snapshot is captured at issue time.
        self.assertEqual(receipt.student_slug, "maya")
        self.assertEqual(receipt.student_name, "Maya Karlsson")
        self.assertEqual(receipt.billing_city, "Wien")

        # Admin deletes the student.
        self.client.force_login(self.admin)
        resp = self.client.delete(f"/api/users/{self.maya.slug}/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(slug="maya").exists())

        # Receipt is retained, detached (FK NULL), snapshot intact.
        receipt.refresh_from_db()
        self.assertIsNone(receipt.student_id)
        self.assertEqual(receipt.student_slug, "maya")
        self.assertEqual(receipt.student_name, "Maya Karlsson")
        self.assertEqual(receipt.billing_postcode, "1010")

    def test_deleting_student_keeps_transactions_and_bookings(self):
        self._grant(3)
        # A held lesson (booking) for maya.
        booking = Booking.objects.create(
            student=self.maya, tutor=self.davit,
            date=current_week_monday(), time="12:00", title="Maya session",
        )
        self.assertEqual(booking.student_slug, "maya")
        self.assertEqual(booking.tutor_slug, "davit")

        # The booking above also auto-recorded its own "book" debit, so scope to
        # the grant transaction created by _grant().
        txn = CreditTransaction.objects.get(student=self.maya, txn_type="buy")
        self.assertEqual(txn.student_slug, "maya")

        self.client.force_login(self.admin)
        self.client.delete(f"/api/users/{self.maya.slug}/")

        booking.refresh_from_db()
        txn.refresh_from_db()
        self.assertIsNone(booking.student_id)
        self.assertEqual(booking.student_slug, "maya")
        self.assertEqual(booking.tutor_slug, "davit")  # tutor still there
        self.assertIsNone(txn.student_id)
        self.assertEqual(txn.student_slug, "maya")

    def test_deleting_tutor_keeps_booking_history(self):
        booking = Booking.objects.create(
            student=self.maya, tutor=self.davit,
            date=current_week_monday(), time="13:00", title="Past session",
        )
        self.client.force_login(self.admin)
        self.client.delete(f"/api/users/{self.davit.slug}/")
        booking.refresh_from_db()
        self.assertIsNone(booking.tutor_id)
        self.assertEqual(booking.tutor_slug, "davit")
        self.assertEqual(booking.tutor_name, "Davit Petrosyan")

    def test_receipt_payload_reads_from_snapshot_after_deletion(self):
        self._grant(2)
        self.client.force_login(self.admin)
        self.client.delete(f"/api/users/{self.maya.slug}/")
        # Admin loads the app: the orphaned receipt must still serialize.
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        receipts = [r for r in payload["receipts"] if r["studentId"] == "maya"]
        self.assertTrue(receipts, "retained receipt must still appear for admin")
        self.assertEqual(receipts[0]["studentName"], "Maya Karlsson")

    def test_receipt_is_frozen_against_later_billing_edits(self):
        self.maya.billing_city = "Wien"
        self.maya.save()
        self._grant(1)
        receipt = Receipt.objects.get(student=self.maya)
        # Student later moves: the issued receipt must not change.
        self.maya.billing_city = "Graz"
        self.maya.save()
        receipt.refresh_from_db()
        self.assertEqual(receipt.billing_city, "Wien")


# --------------------------------------------------------------------------- #
# Stripe self-checkout
# --------------------------------------------------------------------------- #
class StripeCheckoutTests(FluentDataMixin, TestCase):
    def test_checkout_disabled_without_key(self):
        # No STRIPE_SECRET_KEY configured (default): endpoint reports disabled.
        self.client.force_login(self.maya)
        resp = self.client.post(
            "/api/checkout/", data=json.dumps({"n": 5}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 503)

    def test_payload_reports_stripe_disabled_by_default(self):
        self.client.force_login(self.maya)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        self.assertIn("stripe", payload)
        self.assertFalse(payload["stripe"]["enabled"])

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_PUBLISHABLE_KEY="pk_test_x")
    def test_checkout_creates_session(self):
        # A completed account (real e-mail + receipt address) is required to pay.
        complete_payment_setup(self.maya)
        self.client.force_login(self.maya)
        with mock.patch("core.views.stripe") as st:
            st.checkout.Session.create.return_value = mock.Mock(
                url="https://checkout.stripe.test/cs_1", id="cs_1"
            )
            resp = self.client.post(
                "/api/checkout/", data=json.dumps({"n": 5}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["url"], "https://checkout.stripe.test/cs_1")
        # Price is server-authoritative: 5-pack is €145 -> 14500 cents, qty 1.
        _, kwargs = st.checkout.Session.create.call_args
        self.assertEqual(kwargs["line_items"][0]["price_data"]["unit_amount"], 14500)
        self.assertEqual(kwargs["metadata"], {"student_slug": "maya", "credits": "5"})

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_only_students_can_checkout(self):
        self.client.force_login(self.davit)  # tutor
        resp = self.client.post(
            "/api/checkout/", data=json.dumps({"n": 5}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_checkout_requires_billing_address(self):
        # Real e-mail but no billing address -> checkout is blocked.
        self.maya.email = "maya@example.com"
        self.maya.save()
        self.client.force_login(self.maya)
        with mock.patch("core.views.stripe") as st:
            resp = self.client.post(
                "/api/checkout/", data=json.dumps({"n": 5}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "setup_required", "missing": ["billing"]})
        st.checkout.Session.create.assert_not_called()  # no session without an address

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_checkout_requires_real_email(self):
        # Address on file but still the generated placeholder e-mail -> blocked,
        # so Stripe and our receipt mail never go to a dead address.
        complete_payment_setup(self.maya)
        self.maya.email = "maya@fluent.at"
        self.maya.save()
        self.client.force_login(self.maya)
        with mock.patch("core.views.stripe") as st:
            resp = self.client.post(
                "/api/checkout/", data=json.dumps({"n": 5}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "setup_required", "missing": ["email"]})
        st.checkout.Session.create.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_confirm_credits_student_once(self):
        start = self.maya.credits
        session = {
            "id": "cs_paid_1", "payment_status": "paid",
            "metadata": {"student_slug": "maya", "credits": "5"},
        }
        self.client.force_login(self.maya)
        with mock.patch("core.views.stripe") as st:
            st.checkout.Session.retrieve.return_value = session
            r1 = self.client.post(
                "/api/checkout/confirm/", data=json.dumps({"sessionId": "cs_paid_1"}),
                content_type="application/json",
            )
            # Confirm again: must be idempotent (webhook + redirect can both fire).
            r2 = self.client.post(
                "/api/checkout/confirm/", data=json.dumps({"sessionId": "cs_paid_1"}),
                content_type="application/json",
            )
        self.assertTrue(r1.json()["paid"])
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, start + 5)
        self.assertEqual(Receipt.objects.filter(stripe_session_id="cs_paid_1").count(), 1)
        self.assertTrue(r2.json()["paid"])  # second call returns the same receipt

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_confirm_rejects_another_students_session(self):
        session = {
            "id": "cs_x", "payment_status": "paid",
            "metadata": {"student_slug": "ines", "credits": "5"},
        }
        self.client.force_login(self.maya)
        with mock.patch("core.views.stripe") as st:
            st.checkout.Session.retrieve.return_value = session
            resp = self.client.post(
                "/api/checkout/confirm/", data=json.dumps({"sessionId": "cs_x"}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Receipt.objects.filter(stripe_session_id="cs_x").count(), 0)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_webhook_credits_idempotently(self):
        start = self.maya.credits
        event = json.dumps({
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_wh_1", "payment_status": "paid",
                "metadata": {"student_slug": "maya", "credits": "10"},
            }},
        })
        self.client.post("/api/stripe/webhook/", data=event, content_type="application/json",
                         **stripe_signed(event))
        self.client.post("/api/stripe/webhook/", data=event, content_type="application/json",
                         **stripe_signed(event))
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, start + 10)
        self.assertEqual(Receipt.objects.filter(stripe_session_id="cs_wh_1").count(), 1)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_webhook_ignores_unpaid_session(self):
        start = self.maya.credits
        event = json.dumps({
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_unpaid", "payment_status": "unpaid",
                "metadata": {"student_slug": "maya", "credits": "5"},
            }},
        })
        self.client.post("/api/stripe/webhook/", data=event, content_type="application/json",
                         **stripe_signed(event))
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, start)
        self.assertEqual(Receipt.objects.filter(stripe_session_id="cs_unpaid").count(), 0)


# --------------------------------------------------------------------------- #
# Credit purchase cancellation (admin) + Storno receipts + Stripe refunds
# --------------------------------------------------------------------------- #
class CreditCancellationTests(FluentDataMixin, TestCase):
    def _grant(self, n, stripe_session_id=""):
        from core.views import grant_credits, get_settings
        return grant_credits(
            self.maya, n, get_settings(),
            label="Einheiten vom Tutor", sub="Heute · bar bezahlt",
            stripe_session_id=stripe_session_id,
        )

    def _buy_txn(self, receipt):
        return CreditTransaction.objects.get(receipt_no=receipt.number, txn_type="buy")

    def test_admin_cancels_cash_purchase_reverses_credits_and_issues_storno(self):
        start = self.maya.credits
        receipt = self._grant(10)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, start + 10)
        txn = self._buy_txn(receipt)

        self.client.force_login(self.admin)
        resp = self.client.post(f"/api/transactions/{txn.pk}/cancel/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["refundedStripe"])  # cash purchase, no Stripe refund

        # Credits are reversed back to where they started.
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, start)

        # The original purchase is now flagged cancelled and can't be cancelled twice.
        txn.refresh_from_db()
        self.assertTrue(txn.cancelled)

        # A negative Storno receipt was issued, cross-referencing the original.
        storno = Receipt.objects.get(reverses=receipt)
        self.assertEqual(storno.credits, -10)
        self.assertEqual(storno.unit_price_cents, receipt.unit_price_cents)
        self.assertEqual(storno.number, receipt.number.replace("RE-", "ST-", 1))

        # A storno ledger entry visible to the student was created.
        storno_txn = CreditTransaction.objects.get(txn_type="storno", reverses=txn)
        self.assertEqual(storno_txn.amount, -10)
        self.assertEqual(storno_txn.student_id, self.maya.pk)

    def test_double_cancel_is_rejected(self):
        receipt = self._grant(5)
        txn = self._buy_txn(receipt)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(f"/api/transactions/{txn.pk}/cancel/").status_code, 200)
        # Second attempt: already cancelled.
        second = self.client.post(f"/api/transactions/{txn.pk}/cancel/")
        self.assertEqual(second.status_code, 400)
        # Exactly one Storno receipt / one storno ledger row exists.
        self.assertEqual(Receipt.objects.filter(reverses=receipt).count(), 1)
        self.assertEqual(CreditTransaction.objects.filter(txn_type="storno", reverses=txn).count(), 1)

    def test_only_admin_can_cancel(self):
        receipt = self._grant(5)
        txn = self._buy_txn(receipt)
        # Student
        self.client.force_login(self.maya)
        self.assertEqual(self.client.post(f"/api/transactions/{txn.pk}/cancel/").status_code, 403)
        # Tutor
        self.client.force_login(self.davit)
        self.assertEqual(self.client.post(f"/api/transactions/{txn.pk}/cancel/").status_code, 403)
        # Nothing was reversed.
        txn.refresh_from_db()
        self.assertFalse(txn.cancelled)

    def test_non_buy_transaction_is_not_cancellable(self):
        # A booking charge (txn_type="book") must not be cancellable via this path.
        book_txn = CreditTransaction.objects.create(
            student=self.maya, student_slug="maya", student_name="Maya Karlsson",
            txn_type="book", label="Stunde gebucht", amount=-1,
        )
        self.client.force_login(self.admin)
        resp = self.client.post(f"/api/transactions/{book_txn.pk}/cancel/")
        self.assertEqual(resp.status_code, 400)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_stripe_purchase_cancellation_refunds_payment(self):
        receipt = self._grant(10, stripe_session_id="cs_paid_9")
        txn = self._buy_txn(receipt)
        self.client.force_login(self.admin)
        with mock.patch("core.views.stripe") as st:
            st.checkout.Session.retrieve.return_value = {"payment_intent": "pi_9"}
            st.Refund.create.return_value = {"id": "re_9"}
            resp = self.client.post(f"/api/transactions/{txn.pk}/cancel/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["refundedStripe"])
        # The refund was issued against the original session's payment intent...
        st.Refund.create.assert_called_once_with(payment_intent="pi_9")
        # ...and recorded on the Storno receipt.
        storno = Receipt.objects.get(reverses=receipt)
        self.assertEqual(storno.stripe_refund_id, "re_9")

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_stripe_refund_failure_still_cancels(self):
        # A Stripe outage must not block the reversal — the Storno still stands.
        receipt = self._grant(10, stripe_session_id="cs_paid_10")
        txn = self._buy_txn(receipt)
        self.client.force_login(self.admin)
        with mock.patch("core.views.stripe") as st:
            st.checkout.Session.retrieve.side_effect = RuntimeError("stripe down")
            resp = self.client.post(f"/api/transactions/{txn.pk}/cancel/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["refundedStripe"])
        self.assertTrue(Receipt.objects.filter(reverses=receipt).exists())
        txn.refresh_from_db()
        self.assertTrue(txn.cancelled)

    def test_consolidated_ledger_is_admin_only(self):
        self._grant(5)
        # Admin sees the consolidated ledger with the buy entry.
        self.client.force_login(self.admin)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        self.assertIn("allTransactions", payload)
        self.assertTrue(any(t["type"] == "buy" and t["cancellable"] for t in payload["allTransactions"]))
        # Student never receives the consolidated ledger.
        self.client.force_login(self.maya)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        self.assertEqual(payload.get("allTransactions", []), [])


# --------------------------------------------------------------------------- #
# Receipt PDF download
# --------------------------------------------------------------------------- #
class ReceiptPdfTests(FluentDataMixin, TestCase):
    def _grant(self, n):
        from core.views import grant_credits, get_settings
        return grant_credits(self.maya, n, get_settings(), label="Kauf", sub="bar")

    def test_student_downloads_own_receipt_pdf(self):
        receipt = self._grant(5)
        self.client.force_login(self.maya)
        resp = self.client.get(f"/api/receipts/{receipt.number}/pdf/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        body = b"".join(resp.streaming_content) if resp.streaming else resp.content
        self.assertTrue(body.startswith(b"%PDF"), "response must be a real PDF")

    def test_student_cannot_download_another_students_receipt(self):
        receipt = self._grant(5)  # maya's receipt
        self.client.force_login(self.ines)
        resp = self.client.get(f"/api/receipts/{receipt.number}/pdf/")
        self.assertEqual(resp.status_code, 403)

    def test_admin_downloads_any_receipt_pdf(self):
        receipt = self._grant(5)
        self.client.force_login(self.admin)
        resp = self.client.get(f"/api/receipts/{receipt.number}/pdf/")
        self.assertEqual(resp.status_code, 200)

    def test_tutor_downloads_any_receipt_pdf(self):
        # Studio staff manage billing, so a tutor may fetch a student's receipt.
        receipt = self._grant(5)
        self.client.force_login(self.davit)
        resp = self.client.get(f"/api/receipts/{receipt.number}/pdf/")
        self.assertEqual(resp.status_code, 200)

    def test_unknown_receipt_is_404(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/api/receipts/RE-2026-9999/pdf/").status_code, 404)

    def test_pdf_requires_auth(self):
        receipt = self._grant(5)
        self.assertEqual(self.client.get(f"/api/receipts/{receipt.number}/pdf/").status_code, 401)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_ASYNC=False,  # send inline so mail.outbox is populated deterministically
    TUTOR_NOTIFY_EMAIL="studio@thegreenpencil.at",
)
class PurchaseAndCancellationEmailTests(FluentDataMixin, TestCase):
    def _grant(self, n, stripe_session_id=""):
        from core.views import grant_credits, get_settings
        return grant_credits(
            self.maya, n, get_settings(),
            label="Einheiten vom Tutor", sub="Heute · bar bezahlt",
            stripe_session_id=stripe_session_id,
        )

    def _pdf_attachments(self, msg):
        return [a for a in msg.attachments if len(a) == 3 and a[2] == "application/pdf"]

    def test_purchase_notifies_student_and_business(self):
        from django.core import mail
        with self.captureOnCommitCallbacks(execute=True):
            self._grant(10)
        student_mail = [m for m in mail.outbox if m.to == ["maya@fluent.at"]]
        studio_mail = [m for m in mail.outbox if m.to == ["studio@thegreenpencil.at"]]
        self.assertEqual(len(student_mail), 1, "the student must get their receipt")
        self.assertEqual(len(studio_mail), 1, "the business must be notified of the purchase")
        self.assertIn("Beleg", student_mail[0].subject)
        self.assertIn("Neuer Kauf", studio_mail[0].subject)
        # Each notification carries the receipt as a PDF attachment.
        for msg in (student_mail[0], studio_mail[0]):
            pdfs = self._pdf_attachments(msg)
            self.assertEqual(len(pdfs), 1, "the receipt PDF must be attached")
            self.assertTrue(pdfs[0][0].endswith(".pdf"))
            self.assertTrue(pdfs[0][1].startswith(b"%PDF"), "attachment must be a real PDF")

    def test_cancellation_notifies_student_and_business(self):
        from django.core import mail
        receipt = self._grant(10)
        txn = CreditTransaction.objects.get(receipt_no=receipt.number, txn_type="buy")
        mail.outbox = []  # ignore the purchase mails
        self.client.force_login(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(f"/api/transactions/{txn.pk}/cancel/")
        self.assertEqual(resp.status_code, 200)
        student_mail = [m for m in mail.outbox if m.to == ["maya@fluent.at"]]
        studio_mail = [m for m in mail.outbox if m.to == ["studio@thegreenpencil.at"]]
        self.assertEqual(len(student_mail), 1, "the student must hear their purchase was cancelled")
        self.assertEqual(len(studio_mail), 1, "the business must hear about the cancellation")
        self.assertIn("Storniert", student_mail[0].subject)
        self.assertIn("Storniert", studio_mail[0].subject)


# --------------------------------------------------------------------------- #
# Negative credits: tutor books on tab, student/tutor settle the balance
# --------------------------------------------------------------------------- #
@override_settings(EMAIL_ASYNC=False)  # no mail threads racing the test DB
class NegativeCreditBookingTests(FrozenTodayMixin, FluentDataMixin, TestCase):
    def _book(self, actor, student_slug, d="2026-07-01", t="09:30"):
        self.client.force_login(actor)
        return self.client.post(
            "/api/bookings/",
            data=json.dumps({
                "studentSlug": student_slug, "tutorSlug": "davit",
                "date": d, "time": t, "title": "English session",
            }),
            content_type="application/json",
        )

    def test_booking_deducts_one_credit(self):
        start = self.maya.credits
        resp = self._book(self.maya, "maya")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["credits"], start - 1)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, start - 1)
        # The consumption is recorded on the ledger.
        txn = CreditTransaction.objects.filter(student=self.maya, txn_type="book").latest("created_at")
        self.assertEqual(txn.amount, -1)

    def test_tutor_can_book_student_into_negative(self):
        self.ines.credits = 0
        self.ines.save()
        resp = self._book(self.davit, "ines")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["credits"], -1)
        self.ines.refresh_from_db()
        self.assertEqual(self.ines.credits, -1)

    def test_student_cannot_book_without_credits(self):
        self.ines.credits = 0
        self.ines.save()
        resp = self._book(self.ines, "ines")
        self.assertEqual(resp.status_code, 402)
        self.assertEqual(resp.json()["error"], "insufficient_credits")
        self.ines.refresh_from_db()
        self.assertEqual(self.ines.credits, 0)            # no deduction
        self.assertFalse(Booking.objects.filter(student=self.ines, time="09:30").exists())

    def test_student_cannot_book_when_already_negative(self):
        self.maya.credits = -2
        self.maya.save()
        resp = self._book(self.maya, "maya")
        self.assertEqual(resp.status_code, 402)

    def test_credit_floor_blocks_tutor(self):
        s = SiteSettings.objects.first() or SiteSettings.objects.create()
        s.credit_floor = -3
        s.save()
        self.ines.credits = -3                            # already at the floor
        self.ines.save()
        resp = self._book(self.davit, "ines")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"], "credit_floor_reached")
        self.ines.refresh_from_db()
        self.assertEqual(self.ines.credits, -3)           # unchanged

    def test_tutor_delete_refunds_credit(self):
        self.maya.credits = 0
        self.maya.save()
        self._book(self.davit, "maya")                    # -> -1
        b = Booking.objects.filter(student=self.maya, time="09:30").latest("id")
        self.client.force_login(self.davit)
        resp = self.client.delete(f"/api/bookings/{b.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["refunded"])
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, 0)            # back to zero

    def test_student_cancel_within_24h_forfeits(self):
        # A booking later *today* is inside the 24h window -> no refund.
        self.maya.credits = 5
        self.maya.save()
        today = self.FROZEN_NOW.date().isoformat()
        self._book(self.maya, "maya", d=today, t="23:59")
        self.maya.refresh_from_db()
        after_book = self.maya.credits                    # 4
        b = Booking.objects.filter(student=self.maya).latest("id")
        self.client.force_login(self.maya)
        resp = self.client.delete(f"/api/bookings/{b.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["refunded"])
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, after_book)   # forfeited


class BookingSlotValidationTests(FluentDataMixin, TestCase):
    """Slot conflicts/closures must be enforced on the API, not just in the UI."""

    def _post(self, actor, student_slug, d, t):
        self.client.force_login(actor)
        return self.client.post(
            "/api/bookings/",
            data=json.dumps({"studentSlug": student_slug, "tutorSlug": "davit",
                             "date": d, "time": t, "title": "x"}),
            content_type="application/json",
        )

    def test_cannot_double_book_taken_slot(self):
        self._post(self.maya, "maya", "2026-07-02", "09:00")            # maya takes it
        before = Booking.objects.count()
        self.ines.refresh_from_db(); ines_credits = self.ines.credits
        resp = self._post(self.ines, "ines", "2026-07-02", "09:00")     # ines clashes
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"], "slot_taken")
        self.assertEqual(Booking.objects.count(), before)               # no booking
        self.ines.refresh_from_db()
        self.assertEqual(self.ines.credits, ines_credits)               # no deduction

    def test_cannot_book_closed_slot(self):
        AvailabilityOverride.objects.create(
            tutor=self.davit, date=date(2026, 7, 3), time="11:00", is_open=False)
        resp = self._post(self.maya, "maya", "2026-07-03", "11:00")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"], "slot_closed")

    def test_cannot_reschedule_onto_taken_slot(self):
        self._post(self.maya, "maya", "2026-07-04", "09:00")
        self._post(self.ines, "ines", "2026-07-04", "10:00")
        mine = Booking.objects.get(student=self.ines, date=date(2026, 7, 4), time="10:00")
        self.client.force_login(self.ines)
        resp = self.client.put(
            f"/api/bookings/{mine.pk}/",
            data=json.dumps({"time": "09:00"}),                          # collide with maya
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 409)
        mine.refresh_from_db()
        self.assertEqual(mine.time, "10:00")                            # unchanged


# --------------------------------------------------------------------------- #
# Settlement pricing (tier rule) + settle flow
# --------------------------------------------------------------------------- #
class SettlePricingTests(TestCase):
    def setUp(self):
        from core.models import SiteSettings
        self.s = SiteSettings.objects.create(credit_price=30, packs_json=json.dumps([
            {"n": 1, "price": "€32"}, {"n": 5, "price": "€145"}, {"n": 10, "price": "€270"},
        ]))

    def test_tier_unit_price(self):
        from core.views import settle_unit_euros
        # Largest pack whose size <= n sets the per-credit rate.
        self.assertEqual(settle_unit_euros(self.s, 4), 32)    # only the 1-pack reached
        self.assertEqual(settle_unit_euros(self.s, 8), 29)    # 5-pack tier (145/5)
        self.assertEqual(settle_unit_euros(self.s, 13), 27)   # 10-pack tier (270/10)


class SettleFlowTests(FluentDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        SiteSettings.objects.create(credit_price=30, packs_json=json.dumps([
            {"n": 1, "price": "€32"}, {"n": 5, "price": "€145"}, {"n": 10, "price": "€270"},
        ]))
        complete_payment_setup(self.maya)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_student_settle_creates_session_for_outstanding(self):
        self.maya.credits = -8
        self.maya.billing_line1 = "Hauptstraße 1"
        self.maya.billing_postcode = "1010"
        self.maya.billing_city = "Wien"
        self.maya.save()
        self.client.force_login(self.maya)
        with mock.patch("core.views.stripe") as st:
            st.checkout.Session.create.return_value = mock.Mock(url="https://pay/cs_s1", id="cs_s1")
            resp = self.client.post("/api/settle/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["url"], "https://pay/cs_s1")
        _, kwargs = st.checkout.Session.create.call_args
        li = kwargs["line_items"][0]
        self.assertEqual(li["quantity"], 8)
        self.assertEqual(li["price_data"]["unit_amount"], 29 * 100)   # tier rate
        self.assertEqual(kwargs["metadata"], {
            "student_slug": "maya", "credits": "8", "kind": "settle", "unit": "29",
        })

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_settle_rejects_when_nothing_outstanding(self):
        self.client.force_login(self.maya)                # maya has 8 (positive)
        resp = self.client.post("/api/settle/")
        self.assertEqual(resp.status_code, 400)

    def test_tutor_settle_link_requires_debt(self):
        self.client.force_login(self.davit)
        # Positive balance -> nothing to settle.
        resp = self.client.post("/api/students/maya/settle-link/")
        self.assertEqual(resp.status_code, 400)
        # Negative balance but account not set up -> no link (card payment gate).
        self.ines.credits = -5
        self.ines.save()
        resp = self.client.post("/api/students/ines/settle-link/")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "setup_required", "missing": ["email", "billing"]})
        # Account completed -> a token + link is minted.
        complete_payment_setup(self.ines)
        resp = self.client.post("/api/students/ines/settle-link/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.ines.refresh_from_db()
        self.assertTrue(self.ines.settle_token)
        self.assertIn(self.ines.settle_token, body["url"])
        self.assertEqual(body["outstanding"], 5)
        self.assertEqual(body["amount"], 5 * 29)          # 5-pack tier

    def test_students_cannot_mint_settle_link(self):
        self.client.force_login(self.maya)
        resp = self.client.post("/api/students/ines/settle-link/")
        self.assertEqual(resp.status_code, 403)

    def test_public_settle_page_renders_and_validates_token(self):
        self.maya.credits = -8
        self.maya.settle_token = "tok_abc"
        self.maya.save()
        resp = self.client.get("/settle/tok_abc/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Maya")
        self.assertContains(resp, "8 Einheit")
        self.assertContains(resp, "232")                  # 8 × €29
        # Unknown token -> 404 page, no leak.
        self.assertEqual(self.client.get("/settle/nope/").status_code, 404)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_token_checkout_then_webhook_settles_to_zero(self):
        self.maya.credits = -8
        self.maya.settle_token = "tok_pay"
        self.maya.save()
        # Public token checkout creates the session...
        with mock.patch("core.views.stripe") as st:
            st.checkout.Session.create.return_value = mock.Mock(url="https://pay/cs_set", id="cs_set")
            resp = self.client.post("/settle/tok_pay/checkout/")
        self.assertEqual(resp.status_code, 200)
        _, kwargs = st.checkout.Session.create.call_args
        self.assertEqual(kwargs["metadata"]["kind"], "settle")
        # ...and the webhook (source of truth) credits them back to zero, once.
        event = json.dumps({
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_set", "payment_status": "paid",
                "metadata": {"student_slug": "maya", "credits": "8", "kind": "settle", "unit": "29"},
            }},
        })
        self.client.post("/api/stripe/webhook/", data=event, content_type="application/json",
                         **stripe_signed(event))
        self.client.post("/api/stripe/webhook/", data=event, content_type="application/json",
                         **stripe_signed(event))
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, 0)
        r = Receipt.objects.get(stripe_session_id="cs_set")
        self.assertEqual(r.credits, 8)
        self.assertEqual(r.unit_price_cents, 29)          # tier unit on the receipt
        # Paid: the link is retired, but Stripe's return URL still says thanks.
        self.assertEqual(self.client.get("/settle/tok_pay/").status_code, 404)
        self.assertContains(self.client.get("/settle/tok_pay/?paid=1"), "", status_code=200)


class SecurityHardeningTests(FluentDataMixin, TestCase):
    FORGED = json.dumps({"type": "checkout.session.completed", "data": {"object": {
        "id": "cs_forged", "payment_status": "paid",
        "metadata": {"student_slug": "maya", "credits": "50"},
    }}})

    def _post(self, url, body, **extra):
        return self.client.post(url, data=json.dumps(body), content_type="application/json", **extra)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="")
    def test_webhook_is_off_without_a_signing_secret(self):
        r = self.client.post("/api/stripe/webhook/", data=self.FORGED, content_type="application/json")
        self.assertEqual(r.status_code, 503)
        self.assertFalse(Receipt.objects.filter(stripe_session_id="cs_forged").exists())

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_webhook_rejects_a_forged_signature(self):
        r = self.client.post("/api/stripe/webhook/", data=self.FORGED, content_type="application/json",
                             **stripe_signed(self.FORGED, secret="whsec_attacker"))
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Receipt.objects.filter(stripe_session_id="cs_forged").exists())

    def test_login_attempts_are_throttled_per_ip_and_address(self):
        wrong = {"email": "maya@fluent.at", "password": "wrong"}
        for _ in range(10):
            self.assertEqual(self._post("/api/login/", wrong).status_code, 400)
        self.assertEqual(self._post("/api/login/", wrong).status_code, 429)
        # Another address isn't locked out by someone else's guessing.
        self.assertEqual(self._post("/api/login/", {"email": "ines@fluent.at", "password": "x"}).status_code, 400)

    def test_reset_mails_are_capped_per_address(self):
        for _ in range(5):
            self.assertEqual(self._post("/api/password/forgot/", {"email": "maya@fluent.at"}).status_code, 200)
        self.assertEqual(self._post("/api/password/forgot/", {"email": "maya@fluent.at"}).status_code, 429)

    def test_intro_bookings_are_throttled_per_ip(self):
        for _ in range(20):
            self._post("/api/intro-bookings/", {}, HTTP_X_FORWARDED_FOR="203.0.113.7")
        r = self._post("/api/intro-bookings/", {}, HTTP_X_FORWARDED_FOR="203.0.113.7")
        self.assertEqual(r.status_code, 429)
        # A spoofed first hop doesn't buy a fresh counter: the proxy's last entry counts.
        r = self._post("/api/intro-bookings/", {}, HTTP_X_FORWARDED_FOR="198.51.100.1, 203.0.113.7")
        self.assertEqual(r.status_code, 429)

    def test_settings_fail_closed_on_render(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DJANGO_DEBUG", "DJANGO_SECRET_KEY")}
        env["RENDER"] = "true"
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        run = lambda: subprocess.run([sys.executable, "-c", "import fluent.settings as s; print(s.DEBUG)"],
                                     env=env, cwd=root, capture_output=True, text=True)
        r = run()
        self.assertNotEqual(r.returncode, 0, "must refuse to start with the published dev key")
        self.assertIn("DJANGO_SECRET_KEY", r.stderr)
        env["DJANGO_SECRET_KEY"] = "k" * 50
        self.assertEqual(run().stdout.strip(), "False")   # DEBUG defaults off on Render


# --------------------------------------------------------------------------- #
# Buy-credits modal: tutor choice by e-mail (DOM)
# --------------------------------------------------------------------------- #
class DomBuyModalTests(_DomProbeBase):
    def test_single_tutor_mail_targets_that_tutor(self):
        # Only davit exists: no picker, and the e-mail link targets his address.
        r = self.run_probe(self.maya, buy=True)
        self.assertEqual(r["initErrors"], [])
        b = r["buy"]
        self.assertFalse(b["hasTutorSelect"], "one tutor needs no picker")
        self.assertTrue(b["mailHref"].startswith("mailto:davit@fluent.at"),
                        f"mail link must target the tutor: {b['mailHref']}")
        self.assertEqual(b["contactEmail"], "davit@fluent.at")

    def test_multiple_tutors_offer_choice_by_email(self):
        # A second tutor in the system must appear in the picker, listed by e-mail,
        # and the contact e-mail must be a real tutor address (not a hard-coded one).
        make_user("berta", "tutor", first_name="Berta", last_name="Klein", initials="BK")
        r = self.run_probe(self.maya, buy=True)
        self.assertEqual(r["initErrors"], [])
        b = r["buy"]
        self.assertTrue(b["hasTutorSelect"], "two tutors must produce a picker")
        joined = " ".join(b["selectOptions"])
        self.assertIn("davit@fluent.at", joined)
        self.assertIn("berta@fluent.at", joined)
        self.assertIn(b["contactEmail"], {"davit@fluent.at", "berta@fluent.at"})
        self.assertTrue(b["mailHref"].startswith("mailto:"))
        self.assertIn(b["contactEmail"], b["mailHref"])


# --------------------------------------------------------------------------- #
# Public intro-session booking (anonymous, free, no account)
# --------------------------------------------------------------------------- #
class IntroBookingTests(FluentDataMixin, TestCase):
    def _future_date(self, days=3):
        return date.today() + timedelta(days=days)

    def _post(self, **over):
        d = over.pop("date", self._future_date())
        body = {
            "tutorSlug": "davit", "date": d.isoformat(),
            "time": "14:00", "name": "Lena Gast", "email": "lena@example.at",
            "phone": "+43 660 1234567",
        }
        body.update(over)
        return self.client.post(
            "/api/intro-bookings/", data=json.dumps(body),
            content_type="application/json",
        )

    def test_intro_page_is_public(self):
        resp = self.client.get(reverse("intro"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Schnupperstunde")
        # The public payload carries tutors but no student PII.
        self.assertContains(resp, "davit")

    def test_landing_book_cta_points_to_intro_not_app(self):
        html = self.client.get(reverse("landing")).content.decode()
        self.assertIn('href="/intro/"', html)
        # A separate sign-in entry point exists.
        self.assertIn('href="/login/"', html)

    def test_guest_books_free_intro(self):
        start_bookings = Booking.objects.count()
        resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(Booking.objects.count(), start_bookings + 1)
        b = Booking.objects.latest("id")
        self.assertTrue(b.is_intro)
        self.assertIsNone(b.student_id)       # no account
        self.assertEqual(b.tutor.slug, "davit")
        self.assertEqual(b.guest_name, "Lena Gast")
        self.assertEqual(b.guest_email, "lena@example.at")
        self.assertEqual(b.title, "Schnupperstunde (Intro)")

    def test_intro_does_not_touch_credits(self):
        # A free intro must never create a receipt or credit transaction. Measure
        # the delta, since the mixin's seeded booking legitimately adds a debit row.
        receipts_before = Receipt.objects.count()
        txns_before = CreditTransaction.objects.count()
        self._post()
        self.assertEqual(Receipt.objects.count(), receipts_before)
        self.assertEqual(CreditTransaction.objects.count(), txns_before)

    def test_one_intro_per_email_per_tutor(self):
        self.assertEqual(self._post(time="14:00").status_code, 200)
        # Same e-mail, same tutor, different slot -> rejected.
        again = self._post(time="15:00")
        self.assertEqual(again.status_code, 409)

    def test_same_email_can_book_a_different_tutor(self):
        other = make_user(
            "nina", "tutor", first_name="Nina", last_name="Berg", initials="NB"
        )
        self.assertEqual(self._post(time="14:00").status_code, 200)
        # One per tutor: the same guest may still try a second tutor once.
        ok = self._post(tutorSlug=other.slug, time="14:00")
        self.assertEqual(ok.status_code, 200, ok.content)

    def test_booking_error_carries_davit_contact(self):
        self._post(time="14:00")
        again = self._post(time="15:00")
        self.assertIn("davit@thegreenpencil.at", again.json()["error"])
        self.assertIn("397 5535", again.json()["error"])

    def test_cannot_book_taken_slot(self):
        self.assertEqual(self._post(email="a@example.at", time="14:00").status_code, 200)
        # Different guest, same slot -> taken.
        taken = self._post(email="b@example.at", time="14:00")
        self.assertEqual(taken.status_code, 409)

    def test_invalid_email_rejected(self):
        self.assertEqual(self._post(email="not-an-email").status_code, 400)

    def test_unknown_tutor_rejected(self):
        self.assertEqual(self._post(tutorSlug="ghost").status_code, 400)

    def test_past_date_rejected(self):
        resp = self._post(date=date.today() - timedelta(days=1))
        self.assertEqual(resp.status_code, 400)

    def test_closed_slot_rejected(self):
        from core.models import AvailabilityOverride
        d = self._future_date(4)
        AvailabilityOverride.objects.create(
            tutor=self.davit, date=d, time="11:00", is_open=False
        )
        resp = self._post(date=d, time="11:00")
        self.assertEqual(resp.status_code, 409)

    def test_intro_shows_in_tutor_payload_as_guest(self):
        self._post()
        self.client.force_login(self.davit)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        intro = [b for b in payload["bookings"] if b.get("isIntro")]
        self.assertEqual(len(intro), 1)
        self.assertEqual(intro[0]["guestName"], "Lena Gast")
        self.assertEqual(intro[0]["studentId"], "intro")

    def test_student_sees_intro_slot_anonymized(self):
        # A student must not see the guest's identity — just a blocked slot.
        self._post()
        self.client.force_login(self.maya)
        payload = extract_payload(self.client.get(reverse("app")).content.decode())
        intro_like = [b for b in payload["bookings"]
                      if b["time"] == "14:00" and b["studentId"] == "__blocked__"]
        self.assertTrue(intro_like, "intro slot must reach the student as a blocker")
        # No guest identity leaks to the student payload.
        self.assertNotIn("Lena", json.dumps(payload))


# --------------------------------------------------------------------------- #
# Public intro calendar — client-side availability + opening-week behaviour
# (jsdom)
#
# Two bugs guarded here:
#   1. Availability used to be a pseudo-random "seeded" default, so EVERY tutor —
#      including a brand-new one — appeared to have open slots they never set.
#      Availability is now opt-in: a tutor starts with a blank calendar and only
#      explicit overrides (or hand-added custom times) open slots.
#   2. The calendar always opened on the current Mon–Fri week, which on a weekend
#      is entirely in the past, so a visitor landed on a blank calendar even when
#      the tutor had upcoming availability. It now opens on the first upcoming
#      week that actually has a bookable slot.
#
# These run the *real* intro init script with a pinned clock so the behaviour is
# deterministic regardless of the day the suite runs.
# --------------------------------------------------------------------------- #
class IntroCalendarFrontendTests(FluentDataMixin, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._node = _node_bin()
        cls._has_jsdom = _jsdom_installed()

    def _open_davit(self, *days):
        """Mark Davit available at 10:00 & 14:00 on the given 2026 June/July days.
        ``days`` are (month, day) tuples."""
        from core.models import AvailabilityOverride
        for month, day in days:
            for time in ("10:00", "14:00"):
                AvailabilityOverride.objects.create(
                    tutor=self.davit, date=date(2026, month, day), time=time, is_open=True
                )

    def _probe(self, iso_now):
        if not self._node:
            self.skipTest("node not found on PATH — skipping jsdom frontend tests")
        if not self._has_jsdom:
            self.skipTest(
                "jsdom not installed — run `cd tests/frontend && npm install` "
                "to enable frontend tests"
            )
        html = self.client.get(reverse("intro")).content.decode()
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
            fh.write(html)
            path = fh.name
        try:
            out = subprocess.run(
                [self._node, INTRO_PROBE, path, iso_now],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(
                out.returncode, 0, msg=f"probe failed: {out.stderr or out.stdout}"
            )
            return json.loads(out.stdout)
        finally:
            os.unlink(path)

    def test_new_tutor_starts_with_blank_calendar(self):
        # Davit (the default selected tutor) has no overrides and no custom times,
        # so the public calendar must show zero bookable slots — availability is
        # opt-in, never auto-generated.
        r = self._probe("2026-06-24T08:00:00")
        self.assertEqual(r["initErrors"], [], "init must not throw")
        self.assertEqual(
            r["slotCount"], 0,
            "a tutor who set no availability must show no slots",
        )

    def test_explicitly_opened_slots_are_shown(self):
        # Once the tutor opens slots, exactly those appear (2 per opened day).
        self._open_davit((6, 24), (6, 25), (6, 26))
        r = self._probe("2026-06-24T08:00:00")
        self.assertEqual(r["initErrors"], [], "init must not throw")
        self.assertEqual(r["slotCount"], 6)
        self.assertIn("24", r["dayNumbers"])

    def test_weekend_opens_on_first_week_with_slots(self):
        # Sun 28 Jun 2026: the current Mon–Fri week (22–26) is entirely past.
        # Davit is available the following week.
        self._open_davit((6, 29), (6, 30), (7, 1))
        r = self._probe("2026-06-28T12:00:00")
        self.assertEqual(r["initErrors"], [], "init must not throw")
        self.assertGreater(
            r["slotCount"], 0,
            "weekend visitor must not land on a blank calendar",
        )
        # It advanced to the upcoming week (29 Jun–3 Jul), not the elapsed one.
        self.assertIn("29", r["dayNumbers"])
        self.assertNotIn("22", r["dayNumbers"])

    def test_midweek_stays_on_current_week(self):
        # Wed 24 Jun 2026 morning: the current week still has bookable days, so
        # the calendar must not skip ahead and hide today's remaining slots.
        self._open_davit((6, 24), (6, 25), (6, 26))
        r = self._probe("2026-06-24T08:00:00")
        self.assertEqual(r["initErrors"], [], "init must not throw")
        self.assertGreater(r["slotCount"], 0)
        self.assertIn("24", r["dayNumbers"])
        
        
# Transactional e-mail for intro bookings (Resend via Anymail)
# --------------------------------------------------------------------------- #
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_ASYNC=False,  # send inline so mail.outbox is populated deterministically
    EMAIL_REPLY_TO="davit@thegreenpencil.at",
)

class IntroEmailTests(FluentDataMixin, TestCase):
    def _book(self, email="lena@example.at"):
        d = date.today() + timedelta(days=5)
        return self.client.post(
            "/api/intro-bookings/",
            data=json.dumps({
                "tutorSlug": "davit", "date": d.isoformat(),
                "time": "14:00", "name": "Lena Gast", "email": email,
                "phone": "+43 660 1234567",
            }),
            content_type="application/json",
        )

    def test_guest_gets_confirmation_with_ics(self):
        from django.core import mail
        self.assertEqual(self._book().status_code, 200)
        guest = [m for m in mail.outbox if m.to == ["lena@example.at"]]
        self.assertEqual(len(guest), 1)
        msg = guest[0]
        self.assertIn("Schnupperstunde", msg.subject)
        self.assertEqual(msg.reply_to, ["davit@thegreenpencil.at"])
        # multipart: a plaintext body + an HTML alternative
        self.assertIn("Lena", msg.body)
        self.assertTrue(any(ct == "text/html" for _, ct in msg.alternatives))
        # calendar invite attached
        ics = [a for a in msg.attachments if a[0] == "schnupperstunde.ics"]
        self.assertEqual(len(ics), 1)
        self.assertIn("BEGIN:VEVENT", ics[0][1])
        self.assertIn("text/calendar", ics[0][2])

    @override_settings(TUTOR_NOTIFY_EMAIL="studio@thegreenpencil.at")
    def test_tutor_is_notified_when_configured(self):
        from django.core import mail
        self._book()
        tutor_mail = [m for m in mail.outbox if m.to == ["studio@thegreenpencil.at"]]
        self.assertEqual(len(tutor_mail), 1)
        self.assertIn("Lena Gast", tutor_mail[0].subject)
        self.assertIn("lena@example.at", tutor_mail[0].body)

    def test_no_tutor_mail_when_unconfigured(self):
        from django.core import mail
        self._book()  # TUTOR_NOTIFY_EMAIL unset by default
        self.assertEqual(len(mail.outbox), 1)  # guest only

    def test_email_failure_never_breaks_booking(self):
        # Even if the e-mail layer throws, the booking must still succeed.
        with mock.patch("core.emails.send_intro_confirmation", side_effect=RuntimeError("ESP down")):
            resp = self._book()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Booking.objects.filter(is_intro=True, guest_email="lena@example.at").exists())

    def test_ics_event_is_fifteen_minutes(self):
        from core.emails import build_ics
        import re
        d = date.today() + timedelta(days=5)
        b = Booking.objects.create(
            tutor=self.davit, date=d, time="14:00", is_intro=True,
            guest_name="Lena Gast", guest_email="lena@example.at",
            student_name="Lena Gast", student_slug="intro",
        )
        ics = build_ics(b)
        start = re.search(r"DTSTART:(\d{8}T\d{6}Z)", ics).group(1)
        end = re.search(r"DTEND:(\d{8}T\d{6}Z)", ics).group(1)
        from datetime import datetime
        fmt = "%Y%m%dT%H%M%SZ"
        delta = datetime.strptime(end, fmt) - datetime.strptime(start, fmt)
        self.assertEqual(delta, timedelta(minutes=15))


# --------------------------------------------------------------------------- #
# Transactional e-mail for credit (paid) lesson bookings
# --------------------------------------------------------------------------- #
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_ASYNC=False,  # send inline so mail.outbox is populated deterministically
)
class LessonBookingEmailTests(FluentDataMixin, TestCase):
    def _book(self, student_slug="maya", tutor_slug="davit",
              d="2026-06-08", t="09:00", title="English session"):
        self.client.force_login(getattr(self, student_slug))
        return self.client.post(
            "/api/bookings/",
            data=json.dumps({
                "studentSlug": student_slug, "tutorSlug": tutor_slug,
                "date": d, "time": t, "title": title,
            }),
            content_type="application/json",
        )

    def test_tutor_is_notified_at_own_address(self):
        from django.core import mail
        self.assertEqual(self._book().status_code, 200)
        tutor_mail = [m for m in mail.outbox if m.to == ["davit@fluent.at"]]
        self.assertEqual(len(tutor_mail), 1, "the tutor must be e-mailed at their own address")
        msg = tutor_mail[0]
        self.assertIn("Maya Karlsson", msg.subject)
        self.assertIn("Maya Karlsson", msg.body)
        # 45-minute unit reflected in the time range.
        self.assertIn("09:00–09:45", msg.body)
        # Tutor can reply straight to the student.
        self.assertEqual(msg.reply_to, ["maya@fluent.at"])
        # HTML alternative present.
        self.assertTrue(any(ct == "text/html" for _, ct in msg.alternatives))

    def test_student_gets_confirmation_with_cancel_link(self):
        from django.core import mail
        self.assertEqual(self._book().status_code, 200)
        student_mail = [m for m in mail.outbox if m.to == ["maya@fluent.at"]]
        self.assertEqual(len(student_mail), 1, "the student must get a booking confirmation")
        msg = student_mail[0]
        self.assertIn("gebucht", msg.subject)
        # The confirmation carries a public cancel link.
        b = Booking.objects.get(student=self.maya, time="09:00")
        self.assertTrue(b.cancel_token, "a paid booking must mint a cancel token")
        self.assertIn(f"/cancel/{b.cancel_token}/", msg.body)

    def test_no_tutor_mail_when_tutor_has_no_address(self):
        from django.core import mail
        self.davit.email = ""
        self.davit.save()
        self.assertEqual(self._book().status_code, 200)
        # The tutor can't be notified without an address, but the student still
        # gets their confirmation.
        self.assertEqual([m for m in mail.outbox if "Neue Buchung" in m.subject], [])
        self.assertEqual(len([m for m in mail.outbox if m.to == ["maya@fluent.at"]]), 1)

    def test_email_failure_never_breaks_booking(self):
        with mock.patch(
            "core.emails.send_lesson_tutor_notification", side_effect=RuntimeError("ESP down")
        ):
            resp = self._book()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Booking.objects.filter(student=self.maya, time="09:00").exists())

    def test_intro_booking_does_not_trigger_lesson_mail(self):
        # The lesson notifier is a no-op for intros (those have their own flow).
        from django.core import mail
        from core import emails
        b = Booking.objects.create(
            tutor=self.davit, date=date(2026, 6, 8), time="09:00", is_intro=True,
            guest_name="Lena Gast", guest_email="lena@example.at",
        )
        emails.send_lesson_tutor_notification(b.pk)
        self.assertEqual(len(mail.outbox), 0)


# --------------------------------------------------------------------------- #
# Transactional e-mail when a booking is cancelled
# --------------------------------------------------------------------------- #
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_ASYNC=False,  # send inline so mail.outbox is populated deterministically
)
class CancellationEmailTests(FluentDataMixin, TestCase):
    def _future(self, days=5):
        return date.today() + timedelta(days=days)

    def test_lesson_cancel_notifies_student_and_tutor_with_refund(self):
        # Cancel a paid lesson well outside 24h: both sides hear about it and the
        # student's mail confirms the credit came back.
        from django.core import mail
        b = Booking.objects.create(
            student=self.maya, tutor=self.davit, date=self._future(),
            time="09:00", title="English session",
            cancel_token=secrets.token_urlsafe(8),
        )
        self.client.force_login(self.maya)
        resp = self.client.delete(f"/api/bookings/{b.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["refunded"])

        student_mail = [m for m in mail.outbox if m.to == ["maya@fluent.at"]]
        tutor_mail = [m for m in mail.outbox if m.to == ["davit@fluent.at"]]
        self.assertEqual(len(student_mail), 1, "the student must be told their lesson was cancelled")
        self.assertEqual(len(tutor_mail), 1, "the tutor must be told their lesson was cancelled")
        self.assertIn("Storniert", student_mail[0].subject)
        self.assertIn("gutgeschrieben", student_mail[0].body)
        self.assertIn("Maya Karlsson", tutor_mail[0].subject)
        # Tutor can reply straight to the student who cancelled.
        self.assertEqual(tutor_mail[0].reply_to, ["maya@fluent.at"])

    def test_lesson_cancel_within_24h_reports_no_refund(self):
        from django.core import mail
        from django.utils import timezone
        soon = timezone.localtime(timezone.now()).date()
        b = Booking.objects.create(
            student=self.maya, tutor=self.davit, date=soon,
            time="23:59", title="English session",
            cancel_token=secrets.token_urlsafe(8),
        )
        self.client.force_login(self.maya)
        resp = self.client.delete(f"/api/bookings/{b.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["refunded"])
        student_mail = [m for m in mail.outbox if m.to == ["maya@fluent.at"]]
        self.assertEqual(len(student_mail), 1)
        self.assertIn("nicht zurückerstattet", student_mail[0].body)

    @override_settings(TUTOR_NOTIFY_EMAIL="studio@thegreenpencil.at")
    def test_intro_cancel_notifies_guest_and_studio(self):
        # Cancelling an intro via the public tokened link notifies the guest and,
        # when configured, the studio inbox — mirroring the booking notification.
        from django.core import mail
        token = secrets.token_urlsafe(8)
        Booking.objects.create(
            tutor=self.davit, date=self._future(), time="14:00", is_intro=True,
            guest_name="Lena Gast", guest_email="lena@example.at",
            student_name="Lena Gast", student_slug="intro", cancel_token=token,
        )
        resp = self.client.post(f"/cancel/{token}/")
        self.assertEqual(resp.status_code, 200)
        guest_mail = [m for m in mail.outbox if m.to == ["lena@example.at"]]
        studio_mail = [m for m in mail.outbox if m.to == ["studio@thegreenpencil.at"]]
        self.assertEqual(len(guest_mail), 1, "the guest must be told their intro was cancelled")
        self.assertEqual(len(studio_mail), 1, "the studio inbox must hear about the cancellation")
        self.assertIn("Schnupperstunde", guest_mail[0].subject)
        self.assertIn("Lena Gast", studio_mail[0].subject)

    def test_cancel_email_failure_never_breaks_cancellation(self):
        # Even if the mail layer throws, the cancellation itself must succeed.
        b = Booking.objects.create(
            student=self.maya, tutor=self.davit, date=self._future(),
            time="09:00", title="English session",
            cancel_token=secrets.token_urlsafe(8),
        )
        self.client.force_login(self.maya)
        with mock.patch(
            "core.emails.send_cancellation_notifications", side_effect=RuntimeError("ESP down")
        ):
            resp = self.client.delete(f"/api/bookings/{b.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Booking.objects.filter(pk=b.pk).exists())


# --------------------------------------------------------------------------- #
# Video-call connections (Zoom / Teams) + auto-created intro meeting links
# --------------------------------------------------------------------------- #
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_ASYNC=False,  # run the booking follow-up (video + mails) inline
    ZOOM_CLIENT_ID="zoom-app-id",
    ZOOM_CLIENT_SECRET="zoom-app-secret",
)
class VideoConnectionTests(FluentDataMixin, TestCase):
    def _connect_davit(self, provider="zoom"):
        from django.utils import timezone as dj_tz
        from .models import VideoConnection
        return VideoConnection.objects.create(
            tutor=self.davit, provider=provider,
            access_token="live-access-token", refresh_token="live-refresh-token",
            token_expires_at=dj_tz.now() + timedelta(hours=1),
            account_label="davit@zoom.example",
        )

    def _book_intro(self, email="lena@example.at"):
        d = date.today() + timedelta(days=5)
        return self.client.post(
            "/api/intro-bookings/",
            data=json.dumps({
                "tutorSlug": "davit", "date": d.isoformat(),
                "time": "14:00", "name": "Lena Gast", "email": email,
                "phone": "+43 660 1234567",
            }),
            content_type="application/json",
        )

    # ---- OAuth start / callback -------------------------------------------
    def test_oauth_start_requires_a_tutor(self):
        # Students (and anonymous visitors) bounce straight back to the app.
        self.client.force_login(self.maya)
        resp = self.client.get("/oauth/video/zoom/connect/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/app/")

    def test_oauth_start_redirects_to_provider_with_state(self):
        self.client.force_login(self.davit)
        resp = self.client.get("/oauth/video/zoom/connect/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("https://zoom.us/oauth/authorize?"))
        self.assertIn("client_id=zoom-app-id", resp["Location"])
        # The anti-forgery state lives in the session and rides in the URL.
        state = self.client.session["video_oauth"]["state"]
        self.assertTrue(state)
        self.assertIn(state, resp["Location"])

    def test_oauth_start_for_unconfigured_provider_flags_unavailable(self):
        # No TEAMS_* credentials in this test class -> teams is not enabled.
        self.client.force_login(self.davit)
        resp = self.client.get("/oauth/video/teams/connect/")
        self.assertEqual(resp["Location"], "/app/?video=unavailable")

    def test_oauth_callback_stores_the_connection(self):
        from .models import VideoConnection
        self.client.force_login(self.davit)
        self.client.get("/oauth/video/zoom/connect/")
        state = self.client.session["video_oauth"]["state"]
        with mock.patch(
            "core.video.exchange_code",
            return_value={"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600},
        ) as exchange, mock.patch(
            "core.video.fetch_account_label", return_value="davit@zoom.example"
        ):
            resp = self.client.get(
                "/oauth/video/zoom/callback/", {"code": "auth-code", "state": state}
            )
        self.assertEqual(resp["Location"], "/app/?video=connected")
        self.assertEqual(exchange.call_args[0][:2], ("zoom", "auth-code"))
        conn = VideoConnection.objects.get(tutor=self.davit)
        self.assertEqual(conn.provider, "zoom")
        self.assertEqual(conn.access_token, "at-1")
        self.assertEqual(conn.refresh_token, "rt-1")
        self.assertEqual(conn.account_label, "davit@zoom.example")

    def test_oauth_callback_rejects_a_forged_state(self):
        from .models import VideoConnection
        self.client.force_login(self.davit)
        self.client.get("/oauth/video/zoom/connect/")
        with mock.patch("core.video.exchange_code") as exchange:
            resp = self.client.get(
                "/oauth/video/zoom/callback/", {"code": "auth-code", "state": "forged"}
            )
        exchange.assert_not_called()
        self.assertEqual(resp["Location"], "/app/?video=error")
        self.assertFalse(VideoConnection.objects.exists())

    # ---- Intro bookings get a call link ------------------------------------
    def test_intro_booking_creates_meeting_and_mails_the_link(self):
        from django.core import mail
        self._connect_davit()
        with mock.patch(
            "core.video.create_meeting",
            return_value=("https://zoom.us/j/987654321", "987654321"),
        ) as create:
            resp = self._book_intro()
        self.assertEqual(resp.status_code, 200)
        b = Booking.objects.get(is_intro=True, guest_email="lena@example.at")
        self.assertEqual(b.call_link, "https://zoom.us/j/987654321")
        self.assertEqual(b.video_provider, "zoom")
        self.assertEqual(b.video_meeting_id, "987654321")
        # The topic names the guest so the tutor's Zoom calendar is readable.
        self.assertIn("Lena Gast", create.call_args.kwargs["topic"])
        guest = [m for m in mail.outbox if m.to == ["lena@example.at"]]
        self.assertEqual(len(guest), 1)
        self.assertIn("https://zoom.us/j/987654321", guest[0].body)
        ics = [a for a in guest[0].attachments if a[0] == "schnupperstunde.ics"][0]
        self.assertIn("LOCATION:https://zoom.us/j/987654321", ics[1])

    def test_provider_outage_never_breaks_the_booking(self):
        from django.core import mail
        from core.video import VideoError
        self._connect_davit()
        with mock.patch("core.video.create_meeting", side_effect=VideoError("zoom down")):
            resp = self._book_intro()
        self.assertEqual(resp.status_code, 200)
        b = Booking.objects.get(is_intro=True, guest_email="lena@example.at")
        self.assertEqual(b.call_link, "")
        # The confirmation still goes out — just without a link.
        self.assertEqual(len([m for m in mail.outbox if m.to == ["lena@example.at"]]), 1)

    def test_no_connection_means_no_meeting_attempt(self):
        with mock.patch("core.video.create_meeting") as create:
            resp = self._book_intro()
        self.assertEqual(resp.status_code, 200)
        create.assert_not_called()

    def test_cancelling_an_intro_deletes_the_meeting(self):
        self._connect_davit()
        token = secrets.token_urlsafe(8)
        Booking.objects.create(
            tutor=self.davit, date=date.today() + timedelta(days=5), time="14:00",
            is_intro=True, guest_name="Lena Gast", guest_email="lena@example.at",
            student_name="Lena Gast", student_slug="intro", cancel_token=token,
            call_link="https://zoom.us/j/987654321",
            video_provider="zoom", video_meeting_id="987654321",
        )
        with mock.patch("core.video._http", return_value={}) as http:
            resp = self.client.post(f"/cancel/{token}/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Booking.objects.filter(cancel_token=token).exists())
        delete_calls = [c for c in http.call_args_list if c.args[0] == "DELETE"]
        self.assertEqual(len(delete_calls), 1)
        self.assertIn("/meetings/987654321", delete_calls[0].args[1])

    # ---- Connection management API -----------------------------------------
    def test_disconnect_removes_the_connection(self):
        from .models import VideoConnection
        self._connect_davit()
        self.client.force_login(self.davit)
        resp = self.client.delete("/api/video-connection/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(VideoConnection.objects.filter(tutor=self.davit).exists())

    def test_students_cannot_touch_the_connection_api(self):
        self.client.force_login(self.maya)
        self.assertEqual(self.client.get("/api/video-connection/").status_code, 403)
        self.assertEqual(self.client.delete("/api/video-connection/").status_code, 403)

    def test_tokens_never_reach_the_client_payload(self):
        self._connect_davit()
        self.client.force_login(self.davit)
        payload = extract_payload(self.client.get("/app/").content.decode())
        self.assertEqual(
            payload["video"]["connection"],
            {"provider": "zoom", "account": "davit@zoom.example"},
        )
        self.assertTrue(payload["video"]["enabled"]["zoom"])
        # The OAuth tokens are bearer credentials — grep the whole payload.
        blob = json.dumps(payload)
        self.assertNotIn("live-access-token", blob)
        self.assertNotIn("live-refresh-token", blob)


# --------------------------------------------------------------------------- #
# Tutor iCal subscription feed
# --------------------------------------------------------------------------- #
class CalendarFeedTests(FluentDataMixin, TestCase):
    def _mint_link(self, as_user=None, body=None):
        self.client.force_login(as_user or self.davit)
        resp = self.client.post(
            "/api/calendar-link/", data=json.dumps(body or {}),
            content_type="application/json",
        )
        return resp

    def test_unknown_token_is_404(self):
        self.assertEqual(self.client.get("/calendar/not-a-token.ics").status_code, 404)

    def test_link_is_minted_once_and_reused(self):
        r1 = self._mint_link().json()
        self.davit.refresh_from_db()
        self.assertTrue(self.davit.calendar_token)
        self.assertIn(f"/calendar/{self.davit.calendar_token}.ics", r1["url"])
        self.assertTrue(r1["webcalUrl"].startswith("webcal://"))
        # Asking again returns the same URL — the token is stable until rotated.
        r2 = self._mint_link().json()
        self.assertEqual(r1["url"], r2["url"])

    def test_regenerate_revokes_the_old_feed_url(self):
        self._mint_link()
        self.davit.refresh_from_db()
        old = self.davit.calendar_token
        self.assertEqual(self.client.get(f"/calendar/{old}.ics").status_code, 200)
        self._mint_link(body={"regenerate": True})
        self.davit.refresh_from_db()
        self.assertNotEqual(self.davit.calendar_token, old)
        self.assertEqual(self.client.get(f"/calendar/{old}.ics").status_code, 404)
        self.assertEqual(
            self.client.get(f"/calendar/{self.davit.calendar_token}.ics").status_code, 200
        )

    def test_students_cannot_mint_a_feed_link(self):
        resp = self._mint_link(as_user=self.maya)
        self.assertEqual(resp.status_code, 403)

    def test_admin_mints_the_link_for_a_tutor(self):
        self._mint_link(as_user=self.admin, body={"tutorSlug": "davit"})
        self.davit.refresh_from_db()
        self.assertTrue(self.davit.calendar_token)

    def test_feed_contains_only_this_tutors_bookings(self):
        other_tutor = make_user("sara", "tutor", first_name="Sara", last_name="Novak")
        Booking.objects.create(
            student=self.maya, tutor=other_tutor,
            date=current_week_monday(), time="11:00", title="Sara session",
        )
        self._mint_link()
        self.davit.refresh_from_db()
        resp = self.client.get(f"/calendar/{self.davit.calendar_token}.ics")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/calendar"))
        body = resp.content.decode()
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertIn("Ines session", body)      # davit's booking (from the mixin)
        self.assertNotIn("Sara session", body)   # the other tutor's is not leaked

    def test_feed_event_carries_guest_contact_and_call_link(self):
        Booking.objects.create(
            tutor=self.davit, date=date.today() + timedelta(days=3), time="14:00",
            title="Schnupperstunde (Intro)", is_intro=True,
            guest_name="Lena Gast", guest_email="lena@example.at",
            guest_phone="+43 660 1234567",
            student_name="Lena Gast", student_slug="intro",
            call_link="https://zoom.us/j/555",
        )
        self._mint_link()
        self.davit.refresh_from_db()
        body = self.client.get(f"/calendar/{self.davit.calendar_token}.ics").content.decode()
        self.assertIn("Lena Gast", body)
        self.assertIn("lena@example.at", body)
        self.assertIn("LOCATION:https://zoom.us/j/555", body)
        self.assertIn("URL:https://zoom.us/j/555", body)

    def test_feed_is_anonymous_but_capability_guarded(self):
        # No login of any kind — the token alone opens the feed (calendar apps
        # can't authenticate), and without it there is nothing to find.
        self._mint_link()
        self.davit.refresh_from_db()
        self.client.logout()
        resp = self.client.get(f"/calendar/{self.davit.calendar_token}.ics")
        self.assertEqual(resp.status_code, 200)


# --------------------------------------------------------------------------- #
# Retroactive cancellation of completed ("Abgeschlossene") lessons
# --------------------------------------------------------------------------- #
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_ASYNC=False,
)
class RetroactiveCancelTests(FluentDataMixin, TestCase):
    """Undoing a lesson that already shows as completed: the booking goes away,
    the student gets the Einheit back, and a ledger entry naming the actor
    keeps the correction fully transparent — with no cancellation e-mails for
    a lesson that is already in the past."""

    def _past_booking(self, days=7):
        return Booking.objects.create(
            student=self.maya, tutor=self.davit,
            date=date.today() - timedelta(days=days), time="15:00",
            title="Englischstunde", cancel_token=secrets.token_urlsafe(8),
        )

    def test_admin_undo_refunds_and_writes_named_ledger_entry(self):
        from django.core import mail
        b = self._past_booking()  # creating it charges maya one credit
        self.maya.refresh_from_db()
        before = self.maya.credits
        self.client.force_login(self.admin)
        resp = self.client.delete(f"/api/bookings/{b.pk}/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["refunded"])
        self.assertTrue(data["retroactive"])
        self.assertFalse(Booking.objects.filter(pk=b.pk).exists())
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, before + 1)
        txn = CreditTransaction.objects.filter(student=self.maya).latest("id")
        self.assertEqual(txn.label, "Stunde rückwirkend storniert — Einheit erstattet")
        self.assertEqual(txn.amount, 1)
        # Full transparency: the entry names who undid it and which lesson.
        self.assertIn("von Studio Admin", txn.sub)
        self.assertIn("mit Davit", txn.sub)
        self.assertIn("15:00", txn.sub)
        # The echoed txn lets the client mirror the ledger without a reload.
        self.assertEqual(data["txn"]["label"], txn.label)
        self.assertEqual(data["txn"]["amt"], "+1")
        # A bookkeeping correction of a past lesson sends no cancellation mail.
        self.assertEqual(len(mail.outbox), 0)

    def test_future_cancellation_keeps_normal_wording_and_mails(self):
        from django.core import mail
        b = Booking.objects.create(
            student=self.maya, tutor=self.davit,
            date=date.today() + timedelta(days=7), time="15:00",
            title="Englischstunde", cancel_token=secrets.token_urlsafe(8),
        )
        self.client.force_login(self.davit)
        resp = self.client.delete(f"/api/bookings/{b.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["retroactive"])
        txn = CreditTransaction.objects.filter(student=self.maya).latest("id")
        self.assertEqual(txn.label, "Buchung storniert — Einheit erstattet")
        self.assertNotIn("von ", txn.sub)
        # A genuinely upcoming lesson still notifies both sides.
        self.assertGreater(len(mail.outbox), 0)

    def test_student_cannot_refund_their_own_past_lesson(self):
        # The undo is a staff correction — a student deleting their own past
        # booking still forfeits the credit (24h rule), retroactive or not.
        b = self._past_booking()
        self.maya.refresh_from_db()
        before = self.maya.credits
        self.client.force_login(self.maya)
        resp = self.client.delete(f"/api/bookings/{b.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["refunded"])
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, before)


# --------------------------------------------------------------------------- #
# Password lifecycle: forced first-login change + "Passwort vergessen" reset
# --------------------------------------------------------------------------- #
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_ASYNC=False,  # send inline so mail.outbox is populated deterministically
)
class PasswordLifecycleTests(FluentDataMixin, TestCase):
    RESET_LINK_RE = re.compile(r"/password/reset/([^/\s]+)/([^/\s]+)/")

    def _json_post(self, url, payload):
        return self.client.post(url, data=json.dumps(payload), content_type="application/json")

    def _login(self, email, password):
        return self._json_post("/api/login/", {"email": email, "password": password})

    # ---- forced change on first login ------------------------------------- #

    def test_admin_created_account_must_change_password(self):
        self.client.force_login(self.admin)
        resp = self._json_post("/api/users/", {"role": "student", "name": "Nino Neu"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        new_user = User.objects.get(slug=payload["slug"])
        self.assertTrue(new_user.must_change_password)

        # First login with the temp password announces the forced step ...
        fresh = Client()
        resp = fresh.post(
            "/api/login/",
            data=json.dumps({"email": new_user.email, "password": payload["tempPassword"]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["mustChangePassword"])
        # ... and the app stays locked behind it (bounced to the login page,
        # which renders the set-password card for the signed-in user).
        self.assertEqual(fresh.get(reverse("app")).headers["Location"], reverse("login"))
        page = fresh.get(reverse("login"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Neues Passwort festlegen")

    def test_admin_password_overwrite_forces_change(self):
        # Same process as a fresh account: an admin-typed password is a
        # handed-over secret and must be replaced on next login.
        self.client.force_login(self.admin)
        resp = self.client.put(
            f"/api/users/{self.maya.slug}/",
            data=json.dumps({"password": "handed-over-secret"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.maya.refresh_from_db()
        self.assertTrue(self.maya.must_change_password)

    def test_admin_changing_own_password_is_not_flagged(self):
        self.client.force_login(self.admin)
        resp = self.client.put(
            f"/api/users/{self.admin.slug}/",
            data=json.dumps({"password": "my-own-choice-1"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.must_change_password)
        # The admin picked it themselves — their session survives the change.
        self.assertEqual(self.client.get(reverse("app")).status_code, 200)

    def test_forced_change_unlocks_app_and_clears_flag(self):
        self.maya.must_change_password = True
        self.maya.save()
        resp = self._login("maya@fluent.at", "password")
        self.assertTrue(resp.json()["mustChangePassword"])
        resp = self._json_post("/api/password/change/", {"password": "brand-new-pass"})
        self.assertEqual(resp.status_code, 200)
        self.maya.refresh_from_db()
        self.assertFalse(self.maya.must_change_password)
        self.assertTrue(self.maya.check_password("brand-new-pass"))
        # Same session keeps working (update_session_auth_hash) — no re-login.
        self.assertEqual(self.client.get(reverse("app")).status_code, 200)

    def test_change_password_validation(self):
        self.client.force_login(self.maya)
        for bad in ("short7!", "123456789"):
            resp = self._json_post("/api/password/change/", {"password": bad})
            self.assertEqual(resp.status_code, 400)
            self.assertIn("Passwort", resp.json()["error"])

    def test_change_password_requires_auth(self):
        resp = self._json_post("/api/password/change/", {"password": "brand-new-pass"})
        self.assertEqual(resp.status_code, 401)

    def test_forced_change_sends_changed_notice_to_real_email(self):
        from django.core import mail
        self.maya.email = "maya@example.com"
        self.maya.must_change_password = True
        self.maya.save()
        self._login("maya@example.com", "password")
        self._json_post("/api/password/change/", {"password": "brand-new-pass"})
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["maya@example.com"])
        self.assertIn("geändert", msg.subject)
        self.assertIn("davit@thegreenpencil.at", msg.body)

    # ---- "Passwort vergessen" --------------------------------------------- #

    def test_login_page_offers_forgot_password(self):
        resp = self.client.get(reverse("login"))
        self.assertContains(resp, "Passwort vergessen?")

    def test_forgot_mails_a_reset_link(self):
        from django.core import mail
        self.maya.email = "maya@example.com"
        self.maya.save()
        resp = self._json_post("/api/password/forgot/", {"email": "MAYA@example.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["maya@example.com"])
        m = self.RESET_LINK_RE.search(msg.body)
        self.assertIsNotNone(m, "reset link missing from mail body")
        # The e-mailed link renders the set-password form.
        page = self.client.get(f"/password/reset/{m.group(1)}/{m.group(2)}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Neues Passwort festlegen")

    def test_forgot_is_silent_for_unknown_email(self):
        # Same 200 as for a known address — no account enumeration.
        from django.core import mail
        resp = self._json_post("/api/password/forgot/", {"email": "ghost@example.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(len(mail.outbox), 0)

    def test_forgot_never_mails_the_placeholder_address(self):
        # Fresh accounts carry the auto-generated <slug>@fluent.at address until
        # the admin sets the real one; nobody reads that mailbox.
        from django.core import mail
        resp = self._json_post("/api/password/forgot/", {"email": "maya@fluent.at"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_reset_flow_end_to_end(self):
        from django.core import mail
        self.maya.email = "maya@example.com"
        self.maya.must_change_password = True  # e.g. forgot the admin-set password
        self.maya.save()
        self._json_post("/api/password/forgot/", {"email": "maya@example.com"})
        uid, token = self.RESET_LINK_RE.search(mail.outbox[0].body).groups()
        mail.outbox = []

        resp = self._json_post(
            "/api/password/reset/",
            {"uid": uid, "token": token, "password": "fresh-own-pass"},
        )
        self.assertEqual(resp.status_code, 200)
        self.maya.refresh_from_db()
        self.assertTrue(self.maya.check_password("fresh-own-pass"))
        # A self-set password also lifts a pending forced change.
        self.assertFalse(self.maya.must_change_password)
        # Confirmation notice with the "that wasn't me" escalation contact.
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("davit@thegreenpencil.at", mail.outbox[0].body)
        # The link is single-use: the password change invalidated the token.
        resp = self._json_post(
            "/api/password/reset/",
            {"uid": uid, "token": token, "password": "another-pass-x"},
        )
        self.assertEqual(resp.status_code, 400)
        page = self.client.get(f"/password/reset/{uid}/{token}/")
        self.assertContains(page, "Link abgelaufen")
        # And the new password signs in normally.
        resp = self._login("maya@example.com", "fresh-own-pass")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["mustChangePassword"])

    def test_reset_api_rejects_bad_token(self):
        self.maya.email = "maya@example.com"
        self.maya.save()
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode
        uid = urlsafe_base64_encode(force_bytes(self.maya.pk))
        resp = self._json_post(
            "/api/password/reset/",
            {"uid": uid, "token": "not-a-token", "password": "fresh-own-pass"},
        )
        self.assertEqual(resp.status_code, 400)
        self.maya.refresh_from_db()
        self.assertTrue(self.maya.check_password("password"))

    def test_reset_page_with_garbage_link_shows_expired(self):
        page = self.client.get("/password/reset/xxxx/yyyy/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Link abgelaufen")


# --------------------------------------------------------------------------- #
# Tutor custom top-ups: any number of credits for any total (family deals)
# --------------------------------------------------------------------------- #
class CustomTopUpTests(FluentDataMixin, TestCase):
    def _post(self, data, actor=None):
        self.client.force_login(actor or self.davit)
        return self.client.post(
            "/api/credits/maya/", data=json.dumps(data), content_type="application/json",
        )

    def test_custom_total_sets_exact_receipt_amount(self):
        start = self.maya.credits
        resp = self._post({"n": 10, "total": "355,50", "method": "transfer", "note": "Familienangebot"})
        self.assertEqual(resp.status_code, 200)
        r = resp.json()["receipt"]
        self.assertEqual(r["credits"], 10)
        self.assertEqual(r["total"], 355.5)
        self.assertEqual(r["unit"], 35.55)
        receipt = Receipt.objects.get(number=r["no"])
        self.assertEqual(receipt.total_cents, 35550)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.credits, start + 10)
        txn = CreditTransaction.objects.get(receipt_no=r["no"])
        self.assertEqual(txn.label, "Familienangebot")
        self.assertEqual(txn.sub, "Heute · per Überweisung bezahlt")

    def test_standard_top_up_unchanged(self):
        r = self._post({"n": 5}).json()["receipt"]
        receipt = Receipt.objects.get(number=r["no"])
        self.assertIsNone(receipt.total_cents)
        self.assertEqual(r["total"], 5 * receipt.unit_price_cents)
        txn = CreditTransaction.objects.get(receipt_no=r["no"])
        self.assertEqual((txn.label, txn.sub), ("Einheiten vom Tutor", "Heute · bar bezahlt"))

    def test_zero_total_allowed(self):
        r = self._post({"n": 2, "total": "0"}).json()["receipt"]
        self.assertEqual((r["total"], r["unit"]), (0.0, 0.0))

    def test_invalid_input_rejected(self):
        for bad in (
            {"n": 3, "total": "-5"},
            {"n": 3, "total": "12.345"},
            {"n": 3, "total": "abc"},
            {"n": 3, "total": "50001"},
            {"n": 0, "total": "10"},
            {"n": 501, "total": "10"},
            {"n": 3, "total": "10", "method": "crypto"},
        ):
            with self.subTest(bad=bad):
                self.assertEqual(self._post(bad).status_code, 400)
        self.assertFalse(Receipt.objects.filter(student=self.maya).exists())

    def test_students_cannot_grant(self):
        self.assertEqual(self._post({"n": 5, "total": "1"}, actor=self.maya).status_code, 403)

    def test_storno_of_custom_receipt_mirrors_total(self):
        r = self._post({"n": 10, "total": "355.50"}).json()["receipt"]
        receipt = Receipt.objects.get(number=r["no"])
        txn = CreditTransaction.objects.get(receipt_no=r["no"], txn_type="buy")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(f"/api/transactions/{txn.pk}/cancel/").status_code, 200)
        storno = Receipt.objects.get(reverses=receipt)
        self.assertEqual(storno.total_cents, -35550)
        self.assertEqual(storno.amounts(), (35.55, -355.5))

    def test_receipt_pdf_renders_custom_amount(self):
        from .receipts_pdf import render_receipt_pdf
        r = self._post({"n": 3, "total": "100"}).json()["receipt"]
        pdf = render_receipt_pdf(Receipt.objects.get(number=r["no"]))
        self.assertTrue(pdf.startswith(b"%PDF"))


# --------------------------------------------------------------------------- #
# Students set their own real e-mail (needed before card payments)
# --------------------------------------------------------------------------- #
class SelfEmailUpdateTests(FluentDataMixin, TestCase):
    def _put(self, data):
        self.client.force_login(self.maya)
        return self.client.put(
            "/api/users/me/billing/", data=json.dumps(data), content_type="application/json",
        )

    def test_student_sets_real_email(self):
        self.assertEqual(self._put({"email": " Maya@Example.com "}).status_code, 200)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.email, "maya@example.com")

    def test_invalid_email_rejected(self):
        for bad in ("", "not-an-email", "a@b"):
            with self.subTest(bad=bad):
                self.assertEqual(self._put({"email": bad}).status_code, 400)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.email, "maya@fluent.at")

    def test_email_taken_by_someone_else_rejected(self):
        resp = self._put({"email": self.ines.email})
        self.assertEqual(resp.status_code, 400)
        self.maya.refresh_from_db()
        self.assertEqual(self.maya.email, "maya@fluent.at")


class FirstLoginCsrfTests(FluentDataMixin, TestCase):
    """First login with a handed-over password, with CSRF enforced as in prod.
    Logging in rotates the CSRF token, so the token the login page was rendered
    with is stale: the forced password change must use a freshly rendered page
    (the login page reloads to /login/ for exactly this)."""

    def _token(self, client):
        html = client.get("/login/").content.decode()
        return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)

    def test_set_password_after_first_login(self):
        self.maya.must_change_password = True
        self.maya.save()
        c = Client(enforce_csrf_checks=True)
        old = self._token(c)
        resp = c.post("/api/login/", data=json.dumps({"email": self.maya.email, "password": "password"}),
                      content_type="application/json", HTTP_X_CSRFTOKEN=old)
        self.assertTrue(resp.json().get("mustChangePassword"))
        # The pre-login token no longer works — the bug users hit.
        stale = c.post("/api/password/change/", data=json.dumps({"password": "a-new-pass-123"}),
                       content_type="application/json", HTTP_X_CSRFTOKEN=old)
        self.assertEqual(stale.status_code, 403)
        # Reloaded /login/ renders the forced-change form with a fresh token.
        html = c.get("/login/").content.decode()
        self.assertIn('id="setpassForm"', html)
        fresh = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        ok = c.post("/api/password/change/", data=json.dumps({"password": "a-new-pass-123"}),
                    content_type="application/json", HTTP_X_CSRFTOKEN=fresh)
        self.assertEqual(ok.status_code, 200)
        self.maya.refresh_from_db()
        self.assertFalse(self.maya.must_change_password)
