"""Delete or anonymise data the app no longer needs (the retention rules in the
Datenschutzerklärung §8, made automatic). Safe to run daily from a cron job:

    python manage.py cleanup_old_data            # dry run: only reports
    python manage.py cleanup_old_data --apply    # actually cleans up

Rules (see RETENTION below):

* expired login sessions and throttle-cache rows are deleted;
* free intro bookings (Schnupperstunden) lose the guest's name, e-mail, phone
  and notes 12 months after the intro took place;
* receipts, ledger entries and lessons of an account that has been deleted are
  removed once the 7-year retention period of § 132 BAO has run out (counted
  from the end of the calendar year they were created in);
* the tutor's per-day availability tweaks are deleted 90 days after the day;
* student accounts with no login and no lesson for 3 years are only *listed*,
  never touched: deleting a student is the tutor's call.

Receipts and lessons of existing accounts are never deleted by this command.
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Max, Q
from django.utils import timezone

from core.models import (
    AvailabilityOverride, Booking, CreditTransaction, CustomTime, Receipt, User,
)

RETENTION = {
    "intro_guest_days": 365,
    "bao_years": 7,
    "availability_days": 90,
    "inactive_student_days": 3 * 365,
}

ANONYMISED_GUEST = "Schnupperstunde (anonymisiert)"


def _bao_cutoff(now):
    """Records created before this moment are past the § 132 BAO period: it
    runs for 7 years from the end of the calendar year of creation, so a 2025
    receipt may go from 1 Jan 2033."""
    first_deletable_year = now.year - RETENTION["bao_years"] - 1
    return now.replace(year=first_deletable_year + 1, month=1, day=1,
                       hour=0, minute=0, second=0, microsecond=0)


def _expired_cache_rows(now, apply):
    cache_conf = settings.CACHES.get("default", {})
    if cache_conf.get("BACKEND") != "django.core.cache.backends.db.DatabaseCache":
        return 0
    table = connection.ops.quote_name(cache_conf["LOCATION"])
    with connection.cursor() as cursor:
        if apply:
            cursor.execute(f"DELETE FROM {table} WHERE expires < %s", [now])
            return cursor.rowcount
        cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE expires < %s", [now])
        return cursor.fetchone()[0]


def cleanup_old_data(now=None, apply=False):
    """Apply (or, with ``apply=False``, only count) every retention rule.
    Returns ``{rule: count}`` plus ``inactive_students``, a list of slugs."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    report = {}

    with transaction.atomic():
        sessions = Session.objects.filter(expire_date__lt=now)
        report["expired_sessions"] = sessions.count()
        if apply:
            sessions.delete()

        report["expired_cache_rows"] = _expired_cache_rows(now, apply)

        intros = Booking.objects.filter(
            is_intro=True, student__isnull=True,
            date__lt=today - timedelta(days=RETENTION["intro_guest_days"]),
        ).exclude(guest_name="", guest_email="", guest_phone="")
        report["intro_guests_anonymised"] = intros.count()
        if apply:
            intros.update(
                guest_name="", guest_email="", guest_phone="",
                student_name=ANONYMISED_GUEST, notes="", tutor_notes="",
                summary="", homework="", cancel_token="",
            )

        bao_cutoff = _bao_cutoff(now)
        receipts = Receipt.objects.filter(student__isnull=True, created_at__lt=bao_cutoff)
        txns = CreditTransaction.objects.filter(student__isnull=True, created_at__lt=bao_cutoff)
        # Lessons of deleted accounts (intros are handled above). Cascades to
        # the lesson's files and exercises; mistake cards went with the account.
        lessons = Booking.objects.filter(
            is_intro=False, student__isnull=True,
            date__lt=timezone.localdate(bao_cutoff),
        )
        report["expired_receipts"] = receipts.count()
        report["expired_ledger_entries"] = txns.count()
        report["expired_lessons_of_deleted_accounts"] = lessons.count()
        if apply:
            receipts.delete()
            txns.delete()
            lessons.delete()

        availability_cutoff = today - timedelta(days=RETENTION["availability_days"])
        overrides = AvailabilityOverride.objects.filter(date__lt=availability_cutoff)
        custom = CustomTime.objects.filter(date__lt=availability_cutoff)
        report["old_availability_entries"] = overrides.count() + custom.count()
        if apply:
            overrides.delete()
            custom.delete()

    inactive_since = now - timedelta(days=RETENTION["inactive_student_days"])
    report["inactive_students"] = list(
        User.objects.filter(role="student", date_joined__lt=inactive_since)
        .filter(Q(last_login__isnull=True) | Q(last_login__lt=inactive_since))
        .annotate(last_lesson=Max("student_bookings__date"))
        .filter(Q(last_lesson__isnull=True) | Q(last_lesson__lt=inactive_since.date()))
        .order_by("slug")
        .values_list("slug", flat=True)
    )
    return report


class Command(BaseCommand):
    help = "Delete/anonymise data past its retention period (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually delete/anonymise. Without it the command only reports.",
        )

    def handle(self, *args, apply=False, **opts):
        report = cleanup_old_data(apply=apply)
        inactive = report.pop("inactive_students")
        verb = "cleaned" if apply else "would clean (dry run, pass --apply)"
        self.stdout.write(f"cleanup_old_data: {verb}:")
        for rule, count in report.items():
            self.stdout.write(f"  {rule}: {count}")
        if inactive:
            self.stdout.write(
                f"  inactive students (3+ years, not touched; delete in the admin view "
                f"if no longer needed): {', '.join(inactive)}"
            )
