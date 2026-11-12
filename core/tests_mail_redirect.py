"""Test-system mail redirect: no e-mail may leave for a real recipient."""
import json
from datetime import date, timedelta

from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import TestCase, override_settings

from . import emails
from .tests import FluentDataMixin

REDIRECT = dict(
    EMAIL_BACKEND="core.mail_backend.RedirectToTestRecipientBackend",
    EMAIL_DELIVERY_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_ASYNC=False,
    EMAIL_TEST_RECIPIENT="test-inbox@thegreenpencil.at",
    TUTOR_NOTIFY_EMAIL="davit@thegreenpencil.at",
)


@override_settings(**REDIRECT)
class MailRedirectTests(FluentDataMixin, TestCase):
    def _book_intro(self):
        d = date.today() + timedelta(days=5)
        return self.client.post(
            "/api/intro-bookings/",
            data=json.dumps({
                "tutorSlug": "davit", "date": d.isoformat(), "time": "14:00",
                "name": "Lena Gast", "email": "lena@example.at",
                "phone": "+43 660 1234567",
            }),
            content_type="application/json",
        )

    def test_booking_mails_only_reach_test_inbox(self):
        self.assertEqual(self._book_intro().status_code, 200)
        self.assertGreaterEqual(len(mail.outbox), 2)  # guest + tutor
        for msg in mail.outbox:
            self.assertEqual(msg.recipients(), ["test-inbox@thegreenpencil.at"])
        originals = {m.extra_headers["X-Original-To"] for m in mail.outbox}
        self.assertEqual(originals, {"lena@example.at", "davit@thegreenpencil.at"})
        self.assertTrue(any("[an: lena@example.at]" in m.subject for m in mail.outbox))

    def test_password_mail_redirected(self):
        self.maya.email = "maya.k@example.at"
        self.maya.save()
        emails.send_password_changed(self.maya.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].recipients(), ["test-inbox@thegreenpencil.at"])
        self.assertEqual(mail.outbox[0].extra_headers["X-Original-To"], "maya.k@example.at")

    def test_cc_and_bcc_dropped(self):
        msg = EmailMultiAlternatives(
            "Hi", "body", to=["a@example.at"], cc=["b@example.at"], bcc=["c@example.at"],
        )
        self.assertEqual(msg.send(), 1)
        out = mail.outbox[0]
        self.assertEqual((out.to, out.cc, out.bcc), (["test-inbox@thegreenpencil.at"], [], []))
        self.assertEqual(out.extra_headers["X-Original-To"], "a@example.at, b@example.at, c@example.at")

    @override_settings(EMAIL_TEST_RECIPIENT="")
    def test_dropped_when_no_test_recipient(self):
        with self.assertLogs("core.mail_backend", "WARNING"):
            self.assertEqual(self._book_intro().status_code, 200)
        self.assertEqual(mail.outbox, [])
