"""Send a real confirmation-style e-mail to verify the live mail pipeline.

Use after configuring Resend (RESEND_API_KEY + verified domain) to confirm that
From/DKIM/templates/.ics all work end-to-end — without creating a fake booking:

    python manage.py send_test_email --to you@example.com

With no RESEND_API_KEY set, the message is printed to the console instead (the
default backend), which still verifies templates render.
"""
from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import emails
from core.models import Booking


class Command(BaseCommand):
    help = "Send a sample booking-confirmation e-mail to verify the mail setup."

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="Recipient e-mail address.")

    def handle(self, *args, **opts):
        to = opts["to"].strip()
        if "@" not in to:
            raise CommandError("Pass a valid --to address.")

        # A transient (unsaved) booking purely to render the templates + .ics.
        sample = Booking(
            date=date.today() + timedelta(days=3),
            time="14:00",
            title="Schnupperstunde (Intro)",
            is_intro=True,
            guest_name="Test Gast",
            guest_email=to,
            tutor_name="Davit Hovakimyan",
            student_name="Test Gast",
            student_slug="intro",
        )
        msg = emails.intro_confirmation_message(sample)
        msg.subject = f"[Test] {msg.subject}"
        sent = msg.send()

        backend = settings.EMAIL_BACKEND.rsplit(".", 2)[-2]
        self.stdout.write(self.style.SUCCESS(
            f"Sent {sent} message to {to} via '{backend}' backend "
            f"(from {settings.DEFAULT_FROM_EMAIL})."
        ))
        if "console" in settings.EMAIL_BACKEND:
            self.stdout.write(self.style.WARNING(
                "No RESEND_API_KEY set — this was printed, not delivered. "
                "Set the key to send for real."
            ))
