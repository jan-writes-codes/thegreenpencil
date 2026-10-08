"""Auto-decline booking requests the tutor didn't answer in time.

The app also does this whenever bookings are viewed or made; run this from a
cron job (e.g. every 15 minutes) so students hear back even on quiet days:

    python manage.py expire_booking_requests
"""
from django.core.management.base import BaseCommand

from core.views import expire_booking_requests


class Command(BaseCommand):
    help = "Decline booking requests that passed their confirmation deadline."

    def handle(self, *args, **opts):
        n = expire_booking_requests()
        self.stdout.write(f"expire_booking_requests: {n} request(s) declined.")
