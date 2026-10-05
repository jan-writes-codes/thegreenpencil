from datetime import datetime, time as dt_time

from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone


class User(AbstractUser):
    ROLES = [('student', 'Student'), ('tutor', 'Tutor'), ('admin', 'Admin')]
    role = models.CharField(max_length=10, choices=ROLES, default='student')
    slug = models.CharField(max_length=50, unique=True, default='')
    initials = models.CharField(max_length=4, default='')
    credits = models.IntegerField(default=0)
    photo = models.TextField(blank=True, null=True)  # base64 data URL
    color1 = models.CharField(max_length=20, default='#c2714d')
    color2 = models.CharField(max_length=20, default='#a85535')
    billing_name = models.CharField(max_length=200, blank=True)
    billing_line1 = models.CharField(max_length=200, blank=True)
    billing_postcode = models.CharField(max_length=20, blank=True)
    billing_city = models.CharField(max_length=100, blank=True)
    billing_country = models.CharField(max_length=100, default='Österreich')
    receipt_seq = models.IntegerField(default=1000)
    # Capability token for the public "settle outstanding credits" link a tutor
    # can hand to a student. Empty until first generated; a fresh, unguessable
    # value lets the student pay their negative balance without logging in.
    settle_token = models.CharField(max_length=64, blank=True, default='', db_index=True)
    # Capability token for a tutor's private iCal subscription feed
    # (/calendar/<token>.ics). Empty until the tutor first opens the
    # "Kalender abonnieren" dialog; rotating it revokes every old feed URL.
    calendar_token = models.CharField(max_length=64, blank=True, default='', db_index=True)
    # True while the account still runs on a password somebody *else* knows —
    # the temp password from account creation, or one an admin typed in later.
    # The login flow forces the user to pick their own password before they can
    # reach the app; self-set passwords (forced change, e-mail reset) clear it.
    must_change_password = models.BooleanField(default=False)

    class Meta:
        db_table = 'core_user'

    def __str__(self):
        return f'{self.slug} ({self.role})'


class Booking(models.Model):
    # SET_NULL (not CASCADE): a held/completed lesson is part of the immutable
    # history and must survive the deletion of a student or tutor account. The
    # *_slug / *_name snapshots below keep the row self-describing once the FK is
    # detached (GDPR erasure removes the account, the record itself is untouched).
    student = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='student_bookings'
    )
    tutor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='tutor_bookings'
    )
    student_slug = models.CharField(max_length=50, blank=True)
    student_name = models.CharField(max_length=200, blank=True)
    tutor_slug = models.CharField(max_length=50, blank=True)
    tutor_name = models.CharField(max_length=200, blank=True)
    date = models.DateField()
    time = models.CharField(max_length=5)  # "HH:MM"
    title = models.CharField(max_length=200, default='English session')
    notes = models.TextField(blank=True)
    tutor_notes = models.TextField(blank=True)
    call_link = models.TextField(blank=True)
    # Free "intro" session booked by a visitor from the public landing page, who
    # has no account yet. The guest's contact details live here (not on a User),
    # an intro never consumes credits, and it's capped at one per e-mail.
    is_intro = models.BooleanField(default=False)
    guest_name = models.CharField(max_length=200, blank=True)
    guest_email = models.EmailField(blank=True)
    guest_phone = models.CharField(max_length=40, blank=True)
    # Unguessable capability token for the public "cancel this Schnupperstunde"
    # link mailed to both the guest and the tutor — lets either side cancel an
    # intro without logging in. Empty for non-intro bookings.
    cancel_token = models.CharField(max_length=64, blank=True, default='', db_index=True)
    # When call_link was auto-created through the tutor's connected Zoom/Teams
    # account, remember which provider and the provider's meeting id so the
    # meeting can be removed (or moved) again when the booking is cancelled or
    # rescheduled. Empty for hand-pasted links.
    video_provider = models.CharField(max_length=10, blank=True, default='')
    video_meeting_id = models.CharField(max_length=128, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'time']

    def save(self, *args, **kwargs):
        # Detect the initial INSERT *before* super().save() flips the flag, so the
        # credit is charged exactly once — when the booking first comes into being,
        # never on a later reschedule/notes edit.
        is_new = self._state.adding
        # Keep the identity snapshot in sync with the live FK while the accounts
        # exist; once an account is deleted the FK goes NULL and the last-known
        # snapshot is what remains.
        if self.student_id and self.student:
            self.student_slug = self.student.slug
            self.student_name = self.student.get_full_name() or self.student.username
        if self.tutor_id and self.tutor:
            self.tutor_slug = self.tutor.slug
            self.tutor_name = self.tutor.get_full_name() or self.tutor.username
        super().save(*args, **kwargs)
        if is_new:
            self._charge_credit()

    @property
    def minutes(self):
        # A Schnupperstunde runs 15 minutes; a paid lesson is one 45-minute Einheit.
        return 15 if self.is_intro else 45

    @property
    def start(self):
        """Aware start datetime in the studio's timezone (settings.TIME_ZONE)."""
        return timezone.make_aware(datetime.combine(self.date, dt_time.fromisoformat(self.time)))

    def ledger_sub(self):
        """Credit-ledger sub-line: which tutor and when, e.g. 'mit Davit · 01.06.2026 · 09:00'.
        Prefers the frozen snapshot, falling back to the live tutor FK."""
        tutor_name = self.tutor_name or (
            self.tutor.get_full_name() if self.tutor_id and self.tutor else ""
        )
        tutor_first = tutor_name.split(" ")[0]
        when = f"{self.date.strftime('%d.%m.%Y')} · {self.time}"
        return f"mit {tutor_first} · {when}" if tutor_first else when

    def _charge_credit(self):
        """Deduct one credit from the student and record it on the ledger when a
        booking is created. Centralised here (rather than in the booking view) so
        *every* path that brings a Booking into existence — the API, the tutor
        calendar, the seed, the admin, a management shell — stays in sync with the
        student's balance and history. A booking can no longer silently exist
        without having been paid for.

        Policy guards that depend on *who* is booking (a student may not go below
        zero; a tutor may only go to the credit floor) stay in the view, which is
        where the actor is known and a rejection can be returned — this method only
        performs the bookkeeping once a booking is allowed to be created.
        """
        # Free intro sessions and guest bookings (no account) never cost a credit.
        if self.is_intro or not self.student_id:
            return
        student = self.student
        student.credits -= 1
        student.save(update_fields=['credits'])
        CreditTransaction.log(
            student, txn_type='book', label='Stunde gebucht', sub=self.ledger_sub(), amount=-1,
        )

    def __str__(self):
        return f'{self.student_slug} + {self.tutor_slug} on {self.date} at {self.time}'


class VideoConnection(models.Model):
    """A tutor's linked video-conferencing account (Zoom or Microsoft Teams).

    Holds the OAuth tokens obtained when the tutor connects their account from
    the tutor portal. One connection per tutor: connecting a second provider
    replaces the first (a lesson only ever needs one call link). Tokens are
    bearer credentials scoped to creating/removing meetings — they are stored
    server-side only and never serialized into any API payload.
    """
    PROVIDERS = [('zoom', 'Zoom'), ('teams', 'Microsoft Teams')]

    tutor = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='video_connection'
    )
    provider = models.CharField(max_length=10, choices=PROVIDERS)
    access_token = models.TextField()
    refresh_token = models.TextField(blank=True, default='')
    # When the access token expires; refreshed lazily right before an API call.
    token_expires_at = models.DateTimeField(null=True, blank=True)
    # The connected account's e-mail / display name, purely for the settings UI
    # ("Verbunden als …") so the tutor can tell which account they linked.
    account_label = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.tutor.slug} ↔ {self.provider}'


class CreditTransaction(models.Model):
    # SET_NULL keeps the financial ledger intact after the student is deleted;
    # the slug/name snapshot below preserves who the entry belonged to.
    student = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions'
    )
    student_slug = models.CharField(max_length=50, blank=True)
    student_name = models.CharField(max_length=200, blank=True)
    txn_type = models.CharField(max_length=10)  # book, buy, done, storno, open
    label = models.CharField(max_length=200)
    sub = models.CharField(max_length=200, blank=True)
    amount = models.IntegerField(default=0)
    receipt_no = models.CharField(max_length=30, blank=True)
    # A purchase ("buy") an admin has since cancelled. Once set the credits have
    # been reversed and a Storno receipt issued, so it can no longer be cancelled
    # and the admin history stops offering the action. Only meaningful for buys.
    cancelled = models.BooleanField(default=False)
    # For a "storno" entry, the original purchase transaction it reverses. SET_NULL
    # so the reversal record survives even if the original is ever removed.
    reverses = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='reversals'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @classmethod
    def log(cls, student, **fields):
        """A ledger entry stamped with the student's identity snapshot."""
        return cls.objects.create(
            student=student,
            student_slug=student.slug,
            student_name=student.get_full_name() or student.username,
            **fields,
        )

    def __str__(self):
        return f'{self.student_slug}: {self.label}'


class Receipt(models.Model):
    number = models.CharField(max_length=30, unique=True)
    # SET_NULL: an issued receipt is an accounting document with a statutory
    # retention period (§ 132 BAO, 7 years) — it must outlive the account it was
    # issued to. The billing snapshot below freezes the recipient's details at
    # issue time (also the correct invoicing behaviour: a receipt never changes
    # because the customer later edits their address).
    student = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    student_slug = models.CharField(max_length=50, blank=True)
    student_name = models.CharField(max_length=200, blank=True)
    billing_name = models.CharField(max_length=200, blank=True)
    billing_line1 = models.CharField(max_length=200, blank=True)
    billing_postcode = models.CharField(max_length=20, blank=True)
    billing_city = models.CharField(max_length=100, blank=True)
    billing_country = models.CharField(max_length=100, blank=True)
    date_str = models.CharField(max_length=20)
    credits = models.IntegerField()
    unit_price_cents = models.IntegerField()  # in cents to avoid float
    # Stripe Checkout session that paid for this receipt (empty for receipts the
    # tutor added manually). Used to make webhook/redirect crediting idempotent.
    stripe_session_id = models.CharField(max_length=255, blank=True, default='', db_index=True)
    # A Storno (cancellation / credit note) receipt points at the original purchase
    # receipt it reverses; its ``credits`` (and therefore its totals) are negative.
    # NULL for ordinary purchase receipts.
    reverses = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='reversals'
    )
    # The Stripe refund created when a Stripe-paid purchase is cancelled. Empty for
    # cash purchases, when Stripe isn't involved, or if the refund could not be made.
    stripe_refund_id = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # At most one receipt per Stripe session (empty = manual receipts,
            # which are unconstrained). This is the database-level guard that
            # makes webhook + redirect crediting idempotent under a race.
            models.UniqueConstraint(
                fields=['stripe_session_id'],
                condition=~models.Q(stripe_session_id=''),
                name='unique_stripe_session',
            )
        ]

    def __str__(self):
        return self.number


class AvailabilityOverride(models.Model):
    tutor = models.ForeignKey(User, on_delete=models.CASCADE)
    date = models.DateField()
    time = models.CharField(max_length=5)
    is_open = models.BooleanField(default=True)

    class Meta:
        unique_together = ['tutor', 'date', 'time']

    def __str__(self):
        return f'{self.tutor.slug} {self.date} {self.time} {"open" if self.is_open else "closed"}'


class CustomTime(models.Model):
    tutor = models.ForeignKey(User, on_delete=models.CASCADE)
    date = models.DateField()
    time = models.CharField(max_length=5)

    class Meta:
        unique_together = ['tutor', 'date', 'time']

    def __str__(self):
        return f'{self.tutor.slug} {self.date} {self.time}'


class StudentNote(models.Model):
    tutor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notes_created')
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notes_received')
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Note on {self.student.slug} by {self.tutor.slug}'


class ActiveLesson(models.Model):
    student = models.ForeignKey(User, on_delete=models.CASCADE)
    lesson_id = models.CharField(max_length=20)

    class Meta:
        unique_together = ['student', 'lesson_id']

    def __str__(self):
        return f'{self.student.slug}: {self.lesson_id}'


def lesson_upload_path(instance, filename):
    return f'lessons/{instance.lesson_id}/{filename}'


class LessonFile(models.Model):
    """A PDF the tutor attaches to a curriculum lesson (e.g. 'a1-1'). Files are
    shared across all students who have that lesson unlocked."""
    lesson_id = models.CharField(max_length=20, db_index=True)
    file = models.FileField(upload_to=lesson_upload_path)
    original_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        User, null=True, on_delete=models.SET_NULL, related_name='uploaded_lesson_files'
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at']

    def __str__(self):
        return f'{self.lesson_id}: {self.original_name}'


class SiteSettings(models.Model):
    credit_price = models.IntegerField(default=30)  # EUR
    # The most negative a balance may be pushed by a tutor booking a session for a
    # student who is out of credits (students themselves can never go below 0). Acts
    # as a ceiling on unpaid lessons before the student must settle. -10 ≈ ten
    # lessons of trust.
    credit_floor = models.IntegerField(default=-10)
    packs_json = models.TextField(
        default='[{"n":1,"price":"€32","each":"€32 / session","feat":false},'
                '{"n":5,"price":"€145","each":"€29 / session","feat":false},'
                '{"n":10,"price":"€270","each":"€27 / session","feat":true,"tag":"Popular"}]'
    )
    # Which credit pack is highlighted as "Beliebt" on the public pricing grid,
    # identified by its credit count (n). Single source of truth for the popular
    # badge so it can be toggled from the admin without hand-editing packs_json.
    # 0 = no pack highlighted.
    popular_n = models.IntegerField(default=10)

    def __str__(self):
        return f'SiteSettings (credit_price={self.credit_price})'
