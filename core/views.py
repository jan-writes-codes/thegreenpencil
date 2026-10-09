import json
import logging
import mimetypes
import re
import secrets
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from django.conf import settings as dj_settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction as db_transaction
from django.shortcuts import render, redirect
from django.http import JsonResponse, FileResponse, Http404, HttpResponse
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.tokens import default_token_generator
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.utils.translation import get_language, gettext
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.views.generic import TemplateView
from .models import (
    User, Booking, CreditTransaction, Receipt, AvailabilityOverride,
    CustomTime, StudentNote, ActiveLesson, SiteSettings, LessonFile,
    VideoConnection, ErrorCard, SessionExercise, SessionFile, CurriculumTopic,
    SchoolTest,
)
from django.db.models import Prefetch
from . import emails, video
from .ical import build_tutor_feed
from .receipts_pdf import render_receipt_pdf
from .lesson_pdf import lesson_pdf_filename, render_lesson_pdf

logger = logging.getLogger(__name__)

# Stripe is an optional dependency: the app must import and run without it (the
# tutor-mediated purchase flow is always available). When the package is missing
# or no secret key is configured, the Stripe endpoints report "disabled".
try:
    import stripe
except ImportError:  # pragma: no cover - exercised only where stripe isn't installed
    stripe = None

# How many days back a tutor/admin may log a "forgotten" (retroactive) session.
BACKDATE_LIMIT_DAYS = 30

# Uploaded files (Materialien, worksheets, homework) are stored in the database
# so they survive deploys — the database is small (1 GB), so cap each file.
MAX_LESSON_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
AUDIO_EXTS = {"mp3", "m4a", "wav", "ogg"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
DOC_EXTS = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "rtf"}
ALLOWED_LESSON_EXTS = AUDIO_EXTS | IMAGE_EXTS | DOC_EXTS | {"zip"}


def file_ext(name):
    return name.rsplit(".", 1)[-1].lower() if name and "." in name else ""


def file_kind(name):
    e = file_ext(name)
    if e in AUDIO_EXTS:
        return "audio"
    if e in IMAGE_EXTS:
        return "image"
    if e in DOC_EXTS:
        return "doc"
    return "file"


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def compute_initials(name):
    """First letters of the first two words, e.g. 'Jan Heissenberger' -> 'JH'."""
    parts = [p for p in (name or "").strip().split() if p]
    if not parts:
        return "NS"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def serialize_user(u):
    return {
        "id": u.slug,
        "slug": u.slug,
        "name": u.get_full_name() or u.username,
        "email": u.email,
        "initials": u.initials,
        "credits": u.credits,
        "color1": u.color1,
        "color2": u.color2,
        "photo": u.photo,
        "avatarEmoji": u.avatar_emoji,
        "avatarBg": u.avatar_bg,
        "role": u.role,
        "billing": {
            "name": u.billing_name,
            "line1": u.billing_line1,
            "postcode": u.billing_postcode,
            "city": u.billing_city,
            "country": u.billing_country,
        },
    }


def booking_student_slug(b):
    return b.student.slug if b.student_id and b.student else b.student_slug


def booking_tutor_slug(b):
    return b.tutor.slug if b.tutor_id and b.tutor else b.tutor_slug


def serialize_booking(b, *, private=True):
    """``private`` includes the tutor's own notes — never set for a student's view."""
    data = {
        "pk": b.pk,
        "studentId": booking_student_slug(b),
        "tutorId": booking_tutor_slug(b),
        "date": b.date.isoformat(),
        "time": b.time,
        "title": b.title,
        "notes": b.notes,
        "summary": b.summary,
        "homework": b.homework,
        "homeworkDone": b.homework_done,
        "callLink": b.call_link,
        # "requested" until the tutor confirms a student's own booking.
        "status": b.status,
        # Guest "intro" bookings have no student account; the tutor UI shows the
        # guest's name/e-mail from here instead of looking them up in the roster.
        "isIntro": b.is_intro,
        "guestName": b.guest_name,
        "guestEmail": b.guest_email,
        "guestPhone": b.guest_phone,
    }
    if private:
        data["tutorNotes"] = b.tutor_notes
    # Session page content, when the caller prefetched it (see SESSION_PREFETCH).
    cache = getattr(b, "_prefetched_objects_cache", {})
    if "exercises" in cache:
        data["exercises"] = [serialize_exercise(e) for e in b.exercises.all()]
    if "files" in cache:
        data["files"] = [serialize_session_file(f) for f in b.files.all()]
    return data


# Exercises + file metadata for the session pages, without the file bytes.
SESSION_PREFETCH = (
    "exercises",
    Prefetch("files", queryset=SessionFile.objects.defer("data").select_related("uploaded_by")),
)


def serialize_exercise(e):
    return {"id": e.pk, "title": e.title, "description": e.description, "link": e.link, "done": e.done}


def serialize_session_file(f):
    return {
        "id": f.pk,
        "kind": f.kind,
        "name": f.name,
        "ext": file_ext(f.name).upper() or "FILE",
        "size": f.size,
        "by": (f.uploaded_by.get_full_name() if f.uploaded_by_id and f.uploaded_by else ""),
        "url": f"/api/session-files/{f.pk}/",
        # Linked from Lernmaterialien rather than uploaded to this lesson.
        "materialId": f.material_id,
    }


def serialize_school_test(t):
    return {
        "id": t.pk,
        "studentId": t.student.slug,
        "kind": t.kind,
        "date": t.date.isoformat(),
        "topic": t.topic,
        "grade": t.grade,
        "wentWell": t.went_well,
        "toImprove": t.to_improve,
    }


def serialize_error_card(c):
    return {
        "id": c.pk,
        "kind": c.kind,
        "studentId": c.student.slug if c.student_id else "",
        "bookingPk": c.booking_id,
        "front": c.front,
        "back": c.back,
        "note": c.note,
        "status": c.status,
        "reviews": c.reviews,
        "lastResult": c.last_result,
        "created": timezone.localtime(c.created_at).strftime("%Y-%m-%d"),
        "mastered": timezone.localtime(c.mastered_at).strftime("%Y-%m-%d") if c.mastered_at else "",
        # Highlight in a lesson worksheet, if the card was marked there.
        "fileId": c.source_file_id,
        "page": c.page,
        "rects": c.rects or [],
    }


def blocker_booking(b):
    """Anonymized booking sent to a student so the calendar blocks the slot
    without revealing who booked it or any session details."""
    return {
        "pk": None,
        "studentId": "__blocked__",
        "tutorId": booking_tutor_slug(b),
        "date": b.date.isoformat(),
        "time": b.time,
        "title": "",
        "notes": "",
        "tutorNotes": "",
        "callLink": "",
    }


def serialize_transaction(t):
    # Map integer amount to string "+N" / "-N" / ""
    if t.amount > 0:
        amt_str = f"+{t.amount}"
    elif t.amount < 0:
        amt_str = str(t.amount)
    else:
        amt_str = ""
    return {
        "id": t.pk,
        "type": t.txn_type,
        "studentId": t.student_slug,
        "studentName": t.student_name,
        "label": t.label,
        "sub": t.sub,
        "amt": amt_str,
        "receiptNo": t.receipt_no or None,
        "date": t.created_at.strftime("%d.%m.%Y") if t.created_at else "",
        # A purchase (with receipt) or an opening balance may be reversed by an
        # admin; once cancelled the action is gone. The reversing "open" entry
        # itself (negative, reverses set) is never cancellable. Flags are advisory —
        # the server re-checks.
        "cancelled": t.cancelled,
        "cancellable": (
            (t.txn_type == "buy" and bool(t.receipt_no)) or
            (t.txn_type == "open" and t.amount > 0 and t.reverses_id is None)
        ) and not t.cancelled,
        "isStorno": t.txn_type == "storno",
    }


def serialize_receipt(r):
    # Read from the frozen snapshot, not the live user: the receipt must read the
    # same forever, and the student may no longer exist.
    unit, total = r.amounts()
    return {
        "no": r.number,
        "dateStr": r.date_str,
        "studentId": r.student_slug,
        "studentName": r.student_name,
        "billing": {
            "name": r.billing_name,
            "line1": r.billing_line1,
            "postcode": r.billing_postcode,
            "city": r.billing_city,
            "country": r.billing_country,
        },
        "credits": r.credits,
        "unit": unit,
        "net": total,
        "total": total,
        # A Storno (credit note) receipt: negative credits/totals, and a pointer to
        # the original purchase receipt it reverses so both documents cross-reference.
        "isStorno": r.reverses_id is not None,
        "reversesNo": (r.reverses.number if r.reverses_id and r.reverses else ""),
    }


def serialize_topic(t):
    return {"id": t.lesson_id, "level": t.level, "skill": t.skill, "t": t.title}


def serialize_lesson_file(lf):
    return {
        "id": lf.pk,
        "lessonId": lf.lesson_id,
        "name": lf.original_name,
        "ext": file_ext(lf.original_name).upper() or "FILE",
        "kind": file_kind(lf.original_name),
        "url": f"/api/lesson-files/download/{lf.pk}/",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_settings():
    return SiteSettings.objects.first() or SiteSettings.objects.create()


def parse_price(s):
    """Numeric euros from a price string like '€270' / '270,50' (None if absent)."""
    m = re.search(r"\d+(?:[.,]\d+)?", str(s or ""))
    return float(m.group(0).replace(",", ".")) if m else None


def get_packs(settings):
    """The configured credit packs as ``[(n, total_euros or None, raw_dict)]``,
    skipping malformed rows."""
    try:
        raw = json.loads(settings.packs_json)
    except (ValueError, TypeError):
        return []
    packs = []
    for p in raw:
        try:
            packs.append((int(p.get("n")), parse_price(p.get("price")), p))
        except (ValueError, TypeError, AttributeError):
            continue
    return packs


def pack_price_cents(settings, n):
    """Total price (in cents) for a pack of ``n`` credits — server-authoritative so
    a client can never dictate what it pays. Uses the configured pack price when
    ``n`` matches a pack, else falls back to the per-credit rate × n."""
    total = next((t for pn, t, _ in get_packs(settings) if pn == n and t), None)
    return round(total * 100) if total else settings.credit_price * 100 * n


def receipt_unit_price(settings, n):
    """Per-credit price on the receipt for a purchase of n credits: the pack's
    total ÷ n (10 credits -> €270 -> €27), else the per-credit rate."""
    return round(pack_price_cents(settings, n) / 100 / n)


def settle_unit_euros(settings, n):
    """Per-credit price (whole euros) for settling ``n`` outstanding credits: the
    per-credit rate of the *largest configured pack whose size is ≤ n* — i.e. the
    volume tier the student has "already reached". Falls back to the flat
    per-credit price when no pack qualifies. Server-authoritative.

    Example with packs {1:€32, 5:€145, 10:€270}: settling 8 → the 5-pack tier is
    reached (€145/5 = €29) → €29/credit; settling 13 → the 10-pack tier (€27)."""
    tiers = [(pn, t) for pn, t, _ in get_packs(settings) if t and 0 < pn <= n]
    best = max(tiers, key=lambda tier: tier[0], default=None)
    return round(best[1] / best[0] if best else settings.credit_price)


RECEIPT_SEQ_START = 1000


def next_receipt_number(year):
    """Next number in the studio's single, gapless receipt series for ``year``
    (RE-2026-1001, RE-2026-1002, …) — one series across all students, as receipts
    must be. Call inside a transaction: locking the settings row serialises
    concurrent grants so two can't mint the same number."""
    SiteSettings.objects.select_for_update().filter(pk=get_settings().pk).first()
    prefix = f"RE-{year}-"
    seqs = [
        int(no[len(prefix):])
        for no in Receipt.objects.filter(number__startswith=prefix).values_list("number", flat=True)
        if no[len(prefix):].isdigit()
    ]
    return f"{prefix}{max(seqs, default=RECEIPT_SEQ_START) + 1:04d}"


def grant_credits(student, n, settings, *, label, sub, stripe_session_id="", unit_euros=None,
                  total_cents=None):
    """Add ``n`` credits to ``student`` and issue the matching receipt + ledger
    entry, atomically. Shared by the tutor "add credits" action and the Stripe
    checkout flow so both produce identical, auditable records.

    ``unit_euros`` overrides the per-credit price stamped on the receipt (used by
    the settlement flow, where the price follows the tier rule rather than an exact
    pack match); when omitted it falls back to ``receipt_unit_price``.
    ``total_cents`` sets an exact receipt total instead (tutor custom deals); the
    per-credit price shown is then derived from it.

    The receipt and transaction capture a *snapshot* of the student's identity and
    billing address at issue time: these are immutable financial records that must
    stay readable verbatim even after the account is deleted (GDPR erasure).
    """

    with db_transaction.atomic():
        student = User.objects.select_for_update().get(pk=student.pk)
        student.credits += n
        student.save()

        now = timezone.localtime()
        date_str = now.strftime("%d.%m.%Y")
        receipt_no = next_receipt_number(now.year)
        student_name = student.get_full_name() or student.username

        receipt = Receipt.objects.create(
            number=receipt_no,
            student=student,
            student_slug=student.slug,
            student_name=student_name,
            billing_name=student.billing_name,
            billing_line1=student.billing_line1,
            billing_postcode=student.billing_postcode,
            billing_city=student.billing_city,
            billing_country=student.billing_country,
            date_str=date_str,
            credits=n,
            unit_price_cents=(
                round(total_cents / 100 / n) if total_cents is not None
                else unit_euros if unit_euros is not None
                else receipt_unit_price(settings, n)
            ),
            total_cents=total_cents,
            stripe_session_id=stripe_session_id,
        )

        CreditTransaction.log(
            student, txn_type="buy", label=label, sub=sub, amount=n, receipt_no=receipt_no,
        )

    # Notify the student (with their receipt) and the business inbox that a
    # purchase went through. Deferred to on_commit so it only fires once the credit
    # grant (and any enclosing transaction) has durably committed — the sender reads
    # the persisted receipt, and an e-mail failure can never roll back or race the
    # credit write.
    db_transaction.on_commit(lambda: emails.queue_email(emails.send_purchase_notifications, receipt.pk))
    return receipt


def stripe_enabled():
    """True when self-service Stripe checkout is usable (package present + key set)."""
    return bool(stripe and dj_settings.STRIPE_SECRET_KEY)


def stripe_client():
    stripe.api_key = dj_settings.STRIPE_SECRET_KEY
    return stripe


def credit_from_stripe_session(session):
    """Idempotently grant the credits a *paid* Checkout session represents and
    return the resulting Receipt (existing or freshly created), or None if the
    session isn't payable/identifiable. Safe to call from both the webhook and the
    post-payment redirect — the unique constraint on stripe_session_id guarantees a
    session is only ever credited once, even under a race."""
    sid = session.get("id")
    if not sid or session.get("payment_status") != "paid":
        return None
    existing = Receipt.objects.filter(stripe_session_id=sid).first()
    if existing:
        return existing
    meta = session.get("metadata") or {}
    slug = meta.get("student_slug")
    try:
        n = int(meta.get("credits", 0))
    except (ValueError, TypeError):
        n = 0
    if not slug or n <= 0:
        return None
    student = User.objects.filter(slug=slug, role="student").first()
    if not student:
        return None
    # A settlement charge prices each credit by the tier rule, not an exact pack;
    # the unit is carried in metadata so the receipt matches what was charged.
    unit_euros = None
    if meta.get("unit"):
        try:
            unit_euros = int(round(float(meta.get("unit"))))
        except (ValueError, TypeError):
            unit_euros = None
    if meta.get("kind") == "settle":
        label, sub = "Offener Betrag beglichen", "Online bezahlt"
    else:
        label, sub = "Einheiten via Stripe", "Online bezahlt"
    try:
        receipt = grant_credits(
            student, n, get_settings(),
            label=label, sub=sub, stripe_session_id=sid, unit_euros=unit_euros,
        )
    except IntegrityError:
        # A concurrent caller (webhook vs. redirect) won the race; reuse its receipt.
        return Receipt.objects.filter(stripe_session_id=sid).first()
    if meta.get("kind") == "settle":
        # Paid: retire the shared link so it stops showing the student's name and
        # balance to whoever still has it. The tutor can mint a fresh one.
        User.objects.filter(pk=student.pk).update(settle_token="")
    return receipt


def _reverse_credits(locked):
    """Deduct a locked ledger entry's credits back off its student, if the account
    still exists. Returns (student or None, slug, name) for the reversing entry —
    the snapshot from the original row when the account is gone."""
    if locked.student_id is None:
        return None, locked.student_slug, locked.student_name
    student = User.objects.select_for_update().get(pk=locked.student_id)
    student.credits -= locked.amount
    student.save(update_fields=["credits"])
    return student, student.slug, student.get_full_name() or student.username


def cancel_purchase(txn):
    """Reverse a completed credit purchase, atomically and idempotently.

    Deducts the purchased credits back off the student, issues a negative Storno
    (credit-note) receipt cross-referencing the original, records a matching
    ``storno`` ledger entry visible to both the student and the admin, and marks the
    original purchase ``cancelled`` so it can't be reversed twice.

    Returns the created (storno_receipt, storno_txn), or ``None`` if the purchase
    was already cancelled (a concurrent caller won). The Stripe refund, if any, is
    made by the caller afterwards so a payment-provider outage never rolls back the
    bookkeeping."""

    original = Receipt.objects.filter(number=txn.receipt_no).first()
    if not original:
        return None

    with db_transaction.atomic():
        # Lock the purchase row so two concurrent cancels can't both pass the guard.
        locked = CreditTransaction.objects.select_for_update().get(pk=txn.pk)
        if locked.cancelled:
            return None
        n = locked.amount  # credits originally granted (positive)
        student, student_slug, student_name = _reverse_credits(locked)

        now = timezone.localtime()
        # Same running number under an ST- prefix (RE-2026-1001 -> ST-2026-1001).
        # One Storno per purchase (guarded by ``cancelled``), so it stays unique.
        storno_no = "ST-" + original.number.removeprefix("RE-")
        storno_receipt = Receipt.objects.create(
            number=storno_no,
            student=student,
            student_slug=student_slug,
            student_name=student_name,
            # Freeze the original's billing snapshot — a credit note must mirror the
            # document it reverses, not the student's current address.
            billing_name=original.billing_name,
            billing_line1=original.billing_line1,
            billing_postcode=original.billing_postcode,
            billing_city=original.billing_city,
            billing_country=original.billing_country,
            date_str=now.strftime("%d.%m.%Y"),
            credits=-n,
            unit_price_cents=original.unit_price_cents,
            total_cents=(-original.total_cents if original.total_cents is not None else None),
            reverses=original,
        )
        storno_txn = CreditTransaction.objects.create(
            student=student,
            student_slug=student_slug,
            student_name=student_name,
            txn_type="storno",
            label="Kauf storniert",
            sub=f"Storno zu {original.number}",
            amount=-n,
            receipt_no=storno_no,
            reverses=locked,
        )
        locked.cancelled = True
        locked.save(update_fields=["cancelled"])

    return storno_receipt, storno_txn


def refund_stripe_purchase(original, storno_receipt):
    """Refund the Stripe payment behind ``original`` in full and record the refund
    id on the Storno receipt. No-op (returns None) for cash purchases or when Stripe
    is unavailable; swallows provider errors so a failed refund never blocks the
    cancellation — the Storno document still stands and the studio can refund by
    hand."""
    if not original.stripe_session_id or not stripe_enabled():
        return None
    client = stripe_client()
    try:
        session = client.checkout.Session.retrieve(original.stripe_session_id)
        payment_intent = session.get("payment_intent")
        if not payment_intent:
            return None
        refund = client.Refund.create(payment_intent=payment_intent)
        refund_id = refund.get("id")
        if refund_id:
            storno_receipt.stripe_refund_id = refund_id
            storno_receipt.save(update_fields=["stripe_refund_id"])
        return refund_id
    except Exception:
        return None


def finalize_history_snapshots(user):
    """Ensure every history row tied to ``user`` carries its identity snapshot
    before the account is deleted, so SET_NULL never detaches a row to an
    anonymous, unidentifiable state. Idempotent: only fills empty snapshots."""
    name = user.get_full_name() or user.username
    for b in Booking.objects.filter(student=user, student_slug=""):
        b.student_slug, b.student_name = user.slug, name
        b.save(update_fields=["student_slug", "student_name"])
    for b in Booking.objects.filter(tutor=user, tutor_slug=""):
        b.tutor_slug, b.tutor_name = user.slug, name
        b.save(update_fields=["tutor_slug", "tutor_name"])
    CreditTransaction.objects.filter(student=user, student_slug="").update(
        student_slug=user.slug, student_name=name
    )
    # Receipts already snapshot billing at issue time; only patch a missing slug.
    Receipt.objects.filter(student=user, student_slug="").update(
        student_slug=user.slug, student_name=name
    )


def payment_setup_missing(user):
    """What a student still has to fill in before any Stripe payment: a real
    e-mail (Stripe receipts, our Beleg mail) and the receipt address. Empty list
    when the account is ready to pay."""
    missing = []
    if not emails.has_real_email(user):
        missing.append("email")
    if not billing_complete(user):
        missing.append("billing")
    return missing


def setup_required_response(missing):
    return JsonResponse({"error": "setup_required", "missing": missing}, status=400)


def billing_complete(user):
    """True when the student has a usable receipt address on file. The account's
    name supplies the recipient line, so we require the postal fields: street,
    postcode and city. Gate self-service purchases on this so every Stripe receipt
    is issued to a real address."""
    return bool(
        (user.billing_line1 or "").strip()
        and (user.billing_postcode or "").strip()
        and (user.billing_city or "").strip()
    )


def require_roles(*roles):
    """Endpoint guard: 401 if anonymous, 403 if the user's role isn't allowed.

    Centralizes authorization so every mutating endpoint declares exactly who
    may call it, instead of accepting any authenticated user.
    """
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return JsonResponse({"error": "auth"}, status=401)
            if getattr(request.user, "role", None) not in roles:
                return JsonResponse({"error": "forbidden"}, status=403)
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


def _client_ip(request):
    # Behind Render's proxy REMOTE_ADDR is the proxy; it appends the connecting
    # address to X-Forwarded-For, so the last entry is the one a client can't forge.
    return (request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[-1].strip()
            or request.META.get("REMOTE_ADDR", ""))


def _throttled(key, limit, window):
    """Count one hit on ``key``; True once more than ``limit`` hits fall within
    ``window`` seconds of each other (each hit extends the window)."""
    # The default cache is the database (settings.CACHES), so all gunicorn
    # workers share one count and a deploy doesn't reset it.
    hits = cache.get(key, 0) + 1
    cache.set(key, hits, window)
    return hits > limit


TOO_MANY = "Zu viele Versuche. Bitte warte ein paar Minuten."


def acting_tutor(request, slug=None):
    """The tutor a tutor/admin action is attributed to.

    A tutor always acts as themselves. An admin manages every tutor, so it must
    say which one via ``slug``; absent that, fall back to the first tutor (keeps
    single-tutor studios working without the client sending a slug).
    """
    if getattr(request.user, "role", None) == "tutor":
        return request.user
    if slug:
        return User.objects.filter(role="tutor", slug=slug).first()
    return User.objects.filter(role="tutor").first()


def parse_body(request):
    try:
        return json.loads(request.body)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Main app view
# ---------------------------------------------------------------------------

def landing_view(request):
    # Public marketing landing page — the site's front door. No auth required;
    # its CTAs funnel visitors into the booking app (which gates on login).
    # Pricing mirrors the admin-configured credit packs so the public page and
    # the in-app "Einheiten aufladen" screen never drift apart.
    settings = get_settings()
    packs = []
    for n, total, raw in get_packs(settings):
        # The popular badge is driven solely by SiteSettings.popular_n, the single
        # source of truth shared with the in-app pricing screen.
        is_popular = n == settings.popular_n
        packs.append({
            "n": n,
            "price": raw.get("price", ""),
            "per_unit": round(total / n) if total and n > 0 else None,
            "feat": is_popular,
            "tag": gettext("Beliebteste Wahl") if is_popular else "",
        })
    # The popular pack's per-unit price is the headline figure in the pricing
    # copy — it's what nearly every student actually pays.
    featured = next((p for p in packs if p["feat"] and p["per_unit"]), None)
    return render(request, "landing.html", {"packs": packs, "featured": featured})


class LocalizedTemplateView(TemplateView):
    """A content page whose English text lives in its own template
    (templates/en/<name>) rather than in the translation catalogue: the FAQ and
    legal pages are long prose, which reads and edits better as a whole page."""

    def get_template_names(self):
        if get_language() == "en":
            return [f"en/{self.template_name}"]
        return [self.template_name]


def login_view(request):
    # Standalone sign-in page. Authenticated users have no business here —
    # unless they still run on a handed-over password: then this page hosts the
    # forced "pick your own password" step before the app becomes reachable.
    if request.user.is_authenticated:
        if getattr(request.user, "must_change_password", False):
            return render(request, "login.html", {"mode": "force"})
        return redirect("app")
    return render(request, "login.html", {"mode": "login"})


def password_reset_view(request, uidb64, token):
    """Landing page of the e-mailed reset link. The token is checked here only
    to decide which state to render (form vs. 'link expired'); the API endpoint
    re-checks it on submit, so this check is cosmetic, not the security gate."""
    user = _reset_link_user(uidb64, token)
    return render(request, "login.html", {
        "mode": "reset" if user else "reset_invalid",
        "uid": uidb64,
        "token": token,
    })


# Public, crawler-facing pages worth listing in the sitemap. The booking app and
# API endpoints are intentionally excluded — they're gated or non-content.
# Each is listed in German and English (/en/...), with hreflang alternates.
SITEMAP_PATHS = ["/", "/faq/", "/intro/", "/impressum/", "/datenschutz/", "/agb/", "/widerruf/"]
SITEMAP_DE_ONLY = ["/einstufung/"]


def robots_txt(request):
    # Tells search engines they may crawl the site and points them at the
    # sitemap so new/updated pages get discovered faster.
    sitemap_url = request.build_absolute_uri("/sitemap.xml")
    body = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /app/",
        "Disallow: /api/",
        "Disallow: /en/api/",
        f"Sitemap: {sitemap_url}",
        "",
    ])
    return HttpResponse(body, content_type="text/plain")


def sitemap_xml(request):
    # Minimal XML sitemap of the public pages, with absolute URLs. Every page
    # is listed once per language, each entry naming all its language versions.
    def alternates(path):
        return {code: request.build_absolute_uri(("" if code == "de" else f"/{code}") + path)
                for code, _name in dj_settings.LANGUAGES}

    entries = []
    for p in SITEMAP_PATHS:
        alts = alternates(p)
        links = "".join(
            f'<xhtml:link rel="alternate" hreflang="{code}" href="{url}"/>'
            for code, url in alts.items()
        )
        entries += [f"<url><loc>{url}</loc>{links}</url>" for url in alts.values()]
    # German-only public pages: one entry, no alternates.
    entries += [f"<url><loc>{request.build_absolute_uri(p)}</loc></url>" for p in SITEMAP_DE_ONLY]
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:xhtml="http://www.w3.org/1999/xhtml">'
        f"{''.join(entries)}</urlset>"
    )
    return HttpResponse(body, content_type="application/xml")


def _tutor_calendars():
    """Every tutor's availability overrides and custom times, keyed per tutor so
    two calendars never collide: ({tid: {date|time: is_open}}, {tid: {date: [times]}})."""
    availability, custom_times = {}, {}
    for ao in AvailabilityOverride.objects.select_related("tutor"):
        availability.setdefault(ao.tutor.slug, {})[f"{ao.date.isoformat()}|{ao.time}"] = ao.is_open
    for ct in CustomTime.objects.select_related("tutor"):
        custom_times.setdefault(ct.tutor.slug, {}).setdefault(ct.date.isoformat(), []).append(ct.time)
    return availability, custom_times


def public_booking_payload():
    """Public, PII-free data for the anonymous intro-booking calendar: every
    tutor, their availability overrides + custom times, and the slots already
    taken (date/time only, never who booked them)."""
    tutors = list(User.objects.filter(role="tutor").order_by("slug"))
    tutor_payload = [
        {
            "slug": t.slug,
            "name": t.get_full_name() or t.username,
            "firstName": (t.get_full_name() or t.username).split(" ")[0],
            "initials": t.initials,
            "color1": t.color1,
            "color2": t.color2,
            "photo": t.photo,
        }
        for t in tutors
    ]

    availability, custom_times = _tutor_calendars()

    # Anonymized taken slots so the calendar greys them out without leaking who
    # booked. Only today onward matters for booking.
    today = timezone.localdate()
    booked = {}
    for b in Booking.objects.filter(date__gte=today).select_related("tutor"):
        slug = booking_tutor_slug(b)
        if slug:
            booked.setdefault(slug, []).append(f"{b.date.isoformat()}|{b.time}")

    return {
        "tutors": tutor_payload,
        "availability": availability,
        "customTimes": custom_times,
        "booked": booked,
    }


def intro_view(request):
    # Public booking page: anonymous visitors pick a tutor + open slot and book a
    # free intro session. No login required; sign-in is a separate route.
    data = public_booking_payload()
    return render(request, "intro.html", {"intro_data": data})


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_PHOTO_RE = re.compile(r"^data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/]+=*$")


_CEFR = ("A1", "A2", "B1", "B2", "C1")
_SCORE_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")


def _level_check_note(lc):
    """One-line summary of the guest's public level check for the tutor, built
    only from whitelisted values (the client sends structured fields, never
    free text). Empty when the guest didn't take the check."""
    if not isinstance(lc, dict):
        return ""
    level = lc.get("level")
    if level not in _CEFR + ("pre-A1",):
        return ""
    head = "unter A1 (Einstieg)" if level == "pre-A1" else level
    toward = lc.get("toward")
    if toward in _CEFR and toward != level:
        head += f", auf dem Weg zu {toward}"
    parts = [f"Level-Check: {head}"]
    for key, label in (("gv", "Grammatik/Wortschatz"), ("rd", "Lesen")):
        m = _SCORE_RE.match(str(lc.get(key) or ""))
        if m and int(m.group(1)) <= int(m.group(2)) <= 40:
            parts.append(f"{label} {m.group(1)}/{m.group(2)}")
    selfs = [f"{label} {lc.get(key)}" for key, label in
             (("speak", "Sprechen"), ("write", "Schreiben"), ("listen", "Hören"))
             if lc.get(key) in _CEFR]
    if selfs:
        parts.append("Selbsteinschätzung: " + ", ".join(selfs))
    return " · ".join(parts)


def _intro_error(message, status):
    """Guest-facing booking error: always carries Davit's contact details (his
    mobile and e-mail), so guests always have a way to reach a human. The
    endpoint also lives under /en/, where the messages come out in English."""
    contact = gettext("Melde dich gerne bei Davit: +43 676 397 5535 oder davit@thegreenpencil.at")
    return JsonResponse({"error": f"{message} {contact}"}, status=status)


@require_http_methods(["POST"])
def api_intro_booking(request):
    """Public: book a free guest intro session (no account, no credits).

    CSRF-protected like every other POST (the page carries the token). A guest is
    identified only by the name + e-mail they provide here; the booking never
    touches a User row. Capped at one intro per e-mail.
    """
    if _throttled(f"intro:{_client_ip(request)}", 20, 60 * 60):
        return _intro_error(gettext("Zu viele Versuche. Bitte warte ein paar Minuten."), 429)
    expire_booking_requests()  # expired requests free their slots
    data = parse_body(request)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    tutor_slug = (data.get("tutorSlug") or "").strip()
    date_key = data.get("date") or ""
    time_str = (data.get("time") or "").strip()

    if not name:
        return _intro_error(gettext("Bitte gib deinen Namen ein."), 400)
    try:
        validate_email(email)
    except ValidationError:
        return _intro_error(gettext("Bitte gib eine gültige E-Mail-Adresse ein."), 400)
    # Phone is required so the tutor can reach the guest (WhatsApp / callback).
    if len(re.sub(r"[^0-9]", "", phone)) < 6:
        return _intro_error(
            gettext("Bitte gib eine gültige Telefonnummer an (für WhatsApp & Rückfragen)."), 400
        )
    if not _TIME_RE.match(time_str):
        return _intro_error(gettext("Ungültige Uhrzeit."), 400)

    tutor = User.objects.filter(role="tutor", slug=tutor_slug).first()
    if tutor is None:
        return _intro_error(gettext("Tutor nicht gefunden."), 400)
    try:
        booking_date = date.fromisoformat(date_key)
    except (ValueError, AttributeError, TypeError):
        return _intro_error(gettext("Ungültiger Termin."), 400)

    if booking_date < timezone.localdate():
        return _intro_error(gettext("Dieser Termin liegt in der Vergangenheit."), 400)

    # One free intro per e-mail *per tutor* — a guest may try a Schnupperstunde
    # with each tutor once, but not book the same tutor twice.
    if Booking.objects.filter(
        is_intro=True, tutor=tutor, guest_email__iexact=email
    ).exists():
        return _intro_error(
            gettext("Für diese E-Mail wurde bei diesem Tutor bereits eine Schnupperstunde gebucht."),
            409,
        )
    # Slot must be free and not explicitly closed by the tutor.
    conflict = _slot_unavailable(tutor, booking_date, time_str)
    if conflict == "slot_taken":
        return _intro_error(gettext("Dieser Termin ist bereits vergeben."), 409)
    if conflict:
        return _intro_error(gettext("Dieser Termin ist nicht verfügbar."), 409)

    booking = Booking.objects.create(
        tutor=tutor, student=None,
        date=booking_date, time=time_str,
        title="Schnupperstunde (Intro)",
        is_intro=True, guest_name=name, guest_email=email, guest_phone=phone,
        # Snapshot so the tutor calendar can show the guest without a User row.
        student_name=name, student_slug="intro",
        # Capability token for the cancel links in both confirmation e-mails.
        cancel_token=secrets.token_urlsafe(24),
        notes=_level_check_note(data.get("levelCheck")),
    )
    # Fire-and-forget: create the video call (when the tutor has Zoom/Teams
    # connected) and send both confirmations — a hiccup in either must never
    # fail the booking itself. One queued job so the call link exists before
    # the e-mails render.
    emails.queue_email(emails.send_intro_notifications, booking.pk, get_language())
    return JsonResponse({
        "ok": True,
        "tutorName": tutor.get_full_name() or tutor.username,
        "date": booking_date.isoformat(),
        "time": time_str,
    })


@require_http_methods(["GET", "POST"])
def booking_cancel_view(request, token):
    """Public cancel page for a booking (free intro *or* a paid lesson), reached from
    the cancel link in the confirmation e-mails. The unguessable token is the
    capability — no login needed — so either the student/guest or the tutor can
    cancel. GET shows a confirmation prompt; POST performs the cancellation
    (mutations never happen on GET, so an e-mail client prefetching the link can't
    silently cancel the booking).

    A paid lesson follows the same 24h policy as the in-app cancel: the credit is
    refunded when cancelled more than 24h ahead, and forfeited inside that window —
    so the e-mail link can't be used to dodge the forfeit rule."""
    booking = Booking.objects.filter(cancel_token=token).first() if token else None
    if not booking:
        return render(request, "intro_cancel.html", {"state": "gone"})
    # An unconfirmed request is always refunded — it was never a firm booking.
    within_24h = (not booking.is_intro and not booking.is_requested
                  and _booking_within_24h(booking))
    if request.method == "POST":
        when = emails.when(booking, get_language())
        refund_txn, _ = _cancel_booking(booking, forfeit=within_24h)
        return render(request, "intro_cancel.html", {
            "state": "done", "when": when, "is_intro": booking.is_intro,
            "refunded": refund_txn is not None,
        })
    ctx = emails._booking_ctx(booking, gettext("deinem Tutor"), lang=get_language())
    return render(request, "intro_cancel.html", {
        "state": "confirm",
        "is_intro": booking.is_intro,
        "when": ctx["when"],
        "tutor_name": ctx["tutor_first"],
        "within_24h": within_24h,
    })


def app_view(request):
    if not request.user.is_authenticated:
        return redirect("login")
    # Still on a handed-over (temp/admin-set) password: the app stays locked
    # until the user has picked their own. The login page renders the form.
    if getattr(request.user, "must_change_password", False):
        return redirect("login")

    user = request.user
    settings = get_settings()
    expire_booking_requests()

    students = list(User.objects.filter(role="student").order_by("slug"))
    tutors = list(User.objects.filter(role="tutor").order_by("slug"))

    # Scope the payload by role. A logged-in student must not receive other
    # students' PII (credits, billing, transaction logs, notes), but the
    # calendar still needs the tutor's other bookings so overlapping slots are
    # blocked — those are sent anonymized (no identity, no notes).
    is_student = user.role == "student"
    visible_students = [user] if is_student else students

    if is_student:
        own_bookings = list(
            Booking.objects.filter(student=user).select_related("student", "tutor")
            .prefetch_related(*SESSION_PREFETCH)
        )
        other_bookings = list(
            Booking.objects.filter(tutor__role="tutor")
            .exclude(student=user)
            .select_related("tutor")
        )
        # The tutor's private notes stay out of a student's payload.
        bookings_payload = [serialize_booking(b, private=False) for b in own_bookings] + [
            blocker_booking(b) for b in other_bookings
        ]
    else:
        bookings_payload = [
            serialize_booking(b)
            for b in Booking.objects.all().select_related("student", "tutor")
            .prefetch_related(*SESSION_PREFETCH)
        ]

    # Transactions: per visible student slug
    transactions = {}
    for s in visible_students:
        txns = list(s.transactions.all())
        transactions[s.slug] = [serialize_transaction(t) for t in txns]

    # Consolidated ledger for the tutor/admin: every student's credit movements in
    # one chronological list, so the admin can review purchases (and cancel them)
    # without drilling into each student. Students never receive this.
    all_transactions = []
    if not is_student:
        all_transactions = [
            serialize_transaction(t)
            for t in CreditTransaction.objects.all().select_related("student")
        ]

    # Receipts: only the viewer's own for students; all for tutor/admin
    if is_student:
        all_receipts = list(
            Receipt.objects.filter(student=user).select_related("student", "reverses")
        )
    else:
        all_receipts = list(Receipt.objects.all().select_related("student", "reverses"))
    receipts_data = [serialize_receipt(r) for r in all_receipts]

    availability, custom_times = _tutor_calendars()

    # Student notes are tutor-private — never expose them to a student client.
    student_notes = {}
    if not is_student:
        for s in visible_students:
            notes = list(StudentNote.objects.filter(student=s).order_by("-created_at"))
            student_notes[s.slug] = [
                {"date": n.created_at.strftime("%d.%m.%Y"), "text": n.text}
                for n in notes
            ]

    # Error cards (Fehler-Training): a student gets their own, tutor/admin all.
    cards_qs = ErrorCard.objects.select_related("student")
    if is_student:
        cards_qs = cards_qs.filter(student=user)
    error_cards = [serialize_error_card(c) for c in cards_qs]

    # Schularbeiten / tests: a student gets their own, tutor/admin all.
    tests_qs = SchoolTest.objects.select_related("student")
    if is_student:
        tests_qs = tests_qs.filter(student=user)
    school_tests = [serialize_school_test(t) for t in tests_qs]

    # Active lessons: {slug: [lesson_ids]}
    active_lessons = {}
    for s in visible_students:
        ids = list(ActiveLesson.objects.filter(student=s).values_list("lesson_id", flat=True))
        active_lessons[s.slug] = ids

    # Lesson PDFs: {lesson_id: [{id,name,url}]}. A student only receives files for
    # lessons they've unlocked; tutor/admin get the full set to manage.
    lesson_files = {}
    if is_student:
        my_ids = set(active_lessons.get(user.slug, []))
        lf_qs = LessonFile.objects.filter(lesson_id__in=my_ids) if my_ids else LessonFile.objects.none()
    else:
        lf_qs = LessonFile.objects.all()
    for lf in lf_qs:
        lesson_files.setdefault(lf.lesson_id, []).append(serialize_lesson_file(lf))

    # Settings — the "Beliebt" badge is driven solely by SiteSettings.popular_n
    # (the single source of truth shared with the public landing page) rather
    # than by per-pack feat/tag flags inside packs_json, so the in-app
    # "Einheiten aufladen" screen and the marketing page never drift apart.
    packs = [
        dict(raw, n=n, feat=n == settings.popular_n, tag="Beliebt" if n == settings.popular_n else "")
        for n, _, raw in get_packs(settings)
    ]

    django_data = {
        "isAuthenticated": True,
        "role": user.role,
        "currentUserId": user.slug,
        "currentUser": serialize_user(user),
        "students": [serialize_user(s) for s in visible_students],
        "tutors": [serialize_user(t) for t in tutors],
        "bookings": bookings_payload,
        "transactions": transactions,
        "allTransactions": all_transactions,
        "receipts": receipts_data,
        "availability": availability,
        "customTimes": custom_times,
        "studentNotes": student_notes,
        "activeLessons": active_lessons,
        "lessonFiles": lesson_files,
        "topics": [serialize_topic(t) for t in CurriculumTopic.objects.all()],
        "errorCards": error_cards,
        "schoolTests": school_tests,
        "settings": {
            "creditPrice": settings.credit_price,
            "packs": packs,
            "popularN": settings.popular_n,
        },
        "stripe": {
            "enabled": stripe_enabled(),
            "publishableKey": dj_settings.STRIPE_PUBLISHABLE_KEY,
        },
        # Zoom/Teams connect state for the tutor portal. Tokens stay server-
        # side; the client only learns which providers are configured and what
        # the tutor's own connection looks like ("Verbunden als …").
        "video": {
            "enabled": video.enabled_providers(),
            "connection": None,
        },
    }
    if user.role == "tutor":
        conn = VideoConnection.objects.filter(tutor=user).first()
        if conn:
            django_data["video"]["connection"] = {
                "provider": conn.provider, "account": conn.account_label,
            }

    # json_script escapes <, > and &, so user text can't close the <script> tag.
    return render(request, "app.html", {"django_data": django_data, "role": user.role})


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
def api_login(request):
    data = parse_body(request)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email:
        return JsonResponse({"error": "Gib deine E-Mail ein"}, status=400)
    # Brute-force guard, keyed on IP + address so nobody can lock out someone else.
    if _throttled(f"login:{_client_ip(request)}:{email}", 10, 15 * 60):
        return JsonResponse({"error": TOO_MANY}, status=429)
    # Single generic message for both unknown-email and wrong-password so the
    # endpoint can't be used to enumerate which emails have accounts.
    INVALID = "E-Mail oder Passwort ungültig."
    user_obj = User.objects.filter(email__iexact=email).first()
    if user_obj is not None:
        user_obj = authenticate(request, username=user_obj.username, password=password)
    if user_obj is None:
        return JsonResponse({"error": INVALID}, status=400)
    login(request, user_obj)
    return JsonResponse({
        "ok": True,
        "role": user_obj.role,
        "slug": user_obj.slug,
        # True while the account runs on a handed-over (temp/admin-set)
        # password — the client then shows the "pick your own password" step
        # instead of entering the app.
        "mustChangePassword": user_obj.must_change_password,
    })


@require_http_methods(["POST"])
def api_logout(request):
    logout(request)
    return JsonResponse({"ok": True})


def validate_new_password(password):
    """German-language sanity rules for a self-chosen password. Returns an
    error message, or None when the password is acceptable. Deliberately
    minimal (mirrors Django's MinimumLength/NumericPassword validators) —
    the rest of the API validates by hand in German the same way."""
    if not password or len(password) < 8:
        return "Das Passwort muss mindestens 8 Zeichen lang sein."
    if password.isdigit():
        return "Das Passwort darf nicht nur aus Zahlen bestehen."
    return None


def _reset_link_user(uidb64, token):
    """Resolve a reset link's uid+token to a user, or None. Django's token
    generator hashes the current password and last_login into the token, so a
    link stops working the moment the password changes (single-use) or after
    PASSWORD_RESET_TIMEOUT."""
    try:
        uid = urlsafe_base64_decode(uidb64 or "").decode()
        user = User.objects.get(pk=uid)
    except (ValueError, OverflowError, User.DoesNotExist):
        return None
    if not default_token_generator.check_token(user, token or ""):
        return None
    return user


def _set_own_password(user, password):
    """A password the user chose themselves: store it and lift the forced-change
    gate. Callers send the 'password was changed' notice afterwards."""
    user.set_password(password)
    user.must_change_password = False
    user.save()


@require_http_methods(["POST"])
@require_roles("student", "tutor", "admin")
def api_change_password(request):
    """Logged-in user sets their own password — the forced step after first
    login with a handed-over temp password (or after an admin overwrote it)."""
    data = parse_body(request)
    password = data.get("password") or ""
    problem = validate_new_password(password)
    if problem:
        return JsonResponse({"error": problem}, status=400)
    user = request.user
    _set_own_password(user, password)
    # Keep the current session alive across the hash change (every *other*
    # session, e.g. one still holding the temp password, is invalidated).
    update_session_auth_hash(request, user)
    emails.queue_email(emails.send_password_changed, user.pk)
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def api_password_forgot(request):
    """Step 1 of "Passwort vergessen": mail a tokenized reset link. Always
    answers ok — a different reply for unknown addresses would let anyone probe
    which e-mails have accounts (same stance as api_login)."""
    data = parse_body(request)
    email = (data.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"error": "Gib deine E-Mail ein"}, status=400)
    # Caps reset mails per address, so the form can't be used to mail-bomb anyone.
    if _throttled(f"forgot:{email}", 5, 60 * 60):
        return JsonResponse({"error": TOO_MANY}, status=429)
    user = User.objects.filter(email__iexact=email).first()
    if user is not None:
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        reset_url = f"{dj_settings.SITE_URL}/password/reset/{uidb64}/{token}/"
        emails.queue_email(emails.send_password_reset, user.pk, reset_url)
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def api_password_reset(request):
    """Step 2 of "Passwort vergessen": the reset page submits the link's
    uid+token plus the new password. The token is re-checked here — the GET
    page render is not the security gate."""
    data = parse_body(request)
    user = _reset_link_user(data.get("uid"), data.get("token"))
    if user is None:
        return JsonResponse(
            {"error": "Der Link ist ungültig oder abgelaufen. Bitte fordere einen neuen an."},
            status=400,
        )
    password = data.get("password") or ""
    problem = validate_new_password(password)
    if problem:
        return JsonResponse({"error": problem}, status=400)
    _set_own_password(user, password)
    emails.queue_email(emails.send_password_changed, user.pk)
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Bookings API
# ---------------------------------------------------------------------------

def _slot_unavailable(tutor, booking_date, time_str, *, exclude_pk=None):
    """Return an error code if the tutor's slot can't be booked — already taken, or
    explicitly closed by the tutor — else None. Server-side mirror of the browser's
    bookingConflict/slotOpen guards, so the API can't be driven into a double-booking
    (the public intro endpoint already enforces the same rules)."""
    taken = Booking.objects.filter(tutor=tutor, date=booking_date, time=time_str)
    if exclude_pk is not None:
        taken = taken.exclude(pk=exclude_pk)
    if taken.exists():
        return "slot_taken"
    if AvailabilityOverride.objects.filter(
        tutor=tutor, date=booking_date, time=time_str, is_open=False
    ).exists():
        return "slot_closed"
    return None


def _booking_within_24h(b):
    """True when booking ``b`` starts less than 24h from now (or is in the past) —
    the window in which a student forfeits the credit on cancellation."""
    return b.start - timezone.now() < timedelta(hours=24)


def _cancel_booking(b, *, forfeit, label="Buchung storniert, Einheit erstattet",
                    sub=None, notify=True):
    """Delete booking ``b``: refund its credit unless ``forfeit``, mail both sides
    (when ``notify``) and remove its auto-created Zoom/Teams meeting.
    Returns (refund transaction or None, the student's new balance or None)."""
    video_ref = (b.tutor_id, b.video_provider, b.video_meeting_id)
    refund_txn = credits = None
    with db_transaction.atomic():
        if b.student_id:
            locked = User.objects.select_for_update().get(pk=b.student_id)
            if not forfeit:
                locked.credits += 1
                locked.save(update_fields=["credits"])
                refund_txn = CreditTransaction.log(
                    locked, txn_type="buy", label=label, sub=sub or b.ledger_sub(), amount=1,
                )
            credits = locked.credits
        snapshot = emails._cancel_snapshot(b, refunded=refund_txn is not None)
        b.delete()
    if notify:
        emails.queue_email(emails.send_cancellation_notifications, snapshot)
    if video_ref[2]:
        emails.queue_email(video.cleanup_meeting, *video_ref)
    return refund_txn, credits


def decline_request(b, *, reason):
    """Decline a student's booking request: free the slot, refund the credit and
    tell the student why. ``reason`` is "declined" (tutor) or "expired"."""
    snapshot = emails._cancel_snapshot(b, refunded=True)
    label = ("Anfrage abgelehnt, Einheit erstattet" if reason == "declined"
             else "Anfrage nicht bestätigt, Einheit erstattet")
    _cancel_booking(b, forfeit=False, label=label, notify=False)
    emails.queue_email(emails.send_lesson_request_declined, snapshot, reason)


def expire_booking_requests(now=None):
    """Auto-decline requests the tutor hasn't answered by their deadline
    (Booking.request_deadline). Cheap and idempotent: runs whenever bookings are
    read or written, and from the ``expire_booking_requests`` command for cron."""
    now = now or timezone.now()
    horizon = (now + Booking.REQUEST_EXPIRY + timedelta(days=1)).date()
    expired = [
        b for b in Booking.objects.filter(status=Booking.STATUS_REQUESTED, date__lte=horizon)
        if b.request_deadline() <= now
    ]
    for b in expired:
        decline_request(b, reason="expired")
    return len(expired)


def _tutor_may_answer(request, b):
    """The booking's own tutor, or an admin, decides on a request."""
    return request.user.role == "admin" or (
        request.user.role == "tutor" and b.tutor_id == request.user.pk
    )


@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_booking_confirm(request, pk):
    """Tutor confirms a student's booking request -> student gets the confirmation."""
    with db_transaction.atomic():
        b = Booking.objects.select_for_update().filter(pk=pk).first()
        if not b:
            return JsonResponse({"error": "not found"}, status=404)
        if not _tutor_may_answer(request, b):
            return JsonResponse({"error": "forbidden"}, status=403)
        if not b.is_requested:
            return JsonResponse({"ok": True, "status": b.status})
        b.status = Booking.STATUS_CONFIRMED
        b.save(update_fields=["status"])
    emails.queue_email(emails.send_lesson_student_confirmation, b.pk)
    return JsonResponse({"ok": True, "status": b.status})


@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_booking_decline(request, pk):
    """Tutor declines a request: slot freed, credit refunded, student informed."""
    with db_transaction.atomic():
        b = Booking.objects.select_for_update().filter(pk=pk).first()
        if not b:
            return JsonResponse({"error": "not found"}, status=404)
        if not _tutor_may_answer(request, b):
            return JsonResponse({"error": "forbidden"}, status=403)
        if not b.is_requested:
            return JsonResponse({"error": "not_requested"}, status=400)
        decline_request(b, reason="declined")
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
@require_roles("student", "tutor", "admin")
def api_bookings(request):
    data = parse_body(request)
    try:
        student = User.objects.get(slug=data["studentSlug"], role="student")
        tutor = User.objects.get(slug=data["tutorSlug"], role="tutor")
        # A student may only create bookings for themselves; tutor/admin for anyone.
        if request.user.role == "student" and student != request.user:
            return JsonResponse({"error": "forbidden"}, status=403)
        booking_date = date.fromisoformat(data["date"])
        time_str = data["time"]
        if not _TIME_RE.match(str(time_str)):
            raise ValueError("invalid time")
        title = data.get("title", "English session")
    except (KeyError, User.DoesNotExist, ValueError) as e:
        return JsonResponse({"error": str(e)}, status=400)

    # Logging a past ("forgotten") session is a tutor/admin action, bounded to a
    # 30-day look-back so old history can't be silently rewritten. Students never
    # book in the past (their UI only offers future slots).
    today = timezone.localdate()
    if booking_date < today and request.user.role in ("tutor", "admin"):
        if (today - booking_date).days > BACKDATE_LIMIT_DAYS:
            return JsonResponse(
                {"error": "too_far_back", "maxDays": BACKDATE_LIMIT_DAYS}, status=400
            )

    # The slot must be free and open — enforced here, not just in the UI, so the
    # API can't be driven into a clash or onto a closed slot. Expired requests are
    # cleared first so they don't hold a slot past their deadline.
    expire_booking_requests()
    conflict = _slot_unavailable(tutor, booking_date, time_str)
    if conflict:
        return JsonResponse({"error": conflict}, status=409)

    settings = get_settings()
    is_self = request.user.role == "student"
    # Every booking consumes one credit — the deduction and the ledger entry are
    # performed by Booking.save() so no creation path can skip them. The view's job
    # is the policy guard: a student may only book what they have; a tutor/admin may
    # book a student into the negative (an unpaid lesson the student settles later)
    # but not past the configured floor. The check runs under the row lock so a
    # concurrent booking can't slip a balance past the limit between check and save.
    with db_transaction.atomic():
        locked = User.objects.select_for_update().get(pk=student.pk)
        if is_self and locked.credits < 1:
            return JsonResponse({"error": "insufficient_credits"}, status=402)
        if not is_self and locked.credits - 1 < settings.credit_floor:
            return JsonResponse(
                {"error": "credit_floor_reached", "floor": settings.credit_floor},
                status=409,
            )
        b = Booking.objects.create(
            student=locked,
            tutor=tutor,
            date=booking_date,
            time=time_str,
            title=title,
            # A student's own booking is a request the tutor confirms first
            # (slot reserved, credit charged now); tutor/admin bookings are final.
            status=Booking.STATUS_REQUESTED if is_self else Booking.STATUS_CONFIRMED,
            requested_at=timezone.now() if is_self else None,
            # Capability token for the cancel links in the confirmation e-mails.
            cancel_token=secrets.token_urlsafe(24),
        )
        # Booking.save() charged the credit on the locked instance; read it back.
        new_credits = locked.credits

    if is_self:
        # Tell the student the request is in; ask the tutor to confirm it.
        emails.queue_email(emails.send_lesson_request_student, b.pk)
        emails.queue_email(emails.send_lesson_request_tutor, b.pk)
    else:
        # Confirm to the student (with a cancel link) and notify the tutor.
        emails.queue_email(emails.send_lesson_student_confirmation, b.pk)
        emails.queue_email(emails.send_lesson_tutor_notification, b.pk)
    return JsonResponse({"pk": b.pk, "credits": new_credits, "status": b.status})


@require_http_methods(["PUT", "DELETE"])
@require_roles("student", "tutor", "admin")
def api_booking_detail(request, pk):
    try:
        b = Booking.objects.get(pk=pk)
    except Booking.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    # Object-level check: a student may only touch their own bookings (prevents
    # IDOR — editing/cancelling another student's session by guessing its pk).
    is_student = request.user.role == "student"
    if is_student and b.student != request.user:
        return JsonResponse({"error": "forbidden"}, status=403)

    if request.method == "DELETE":
        # A booking whose day is already over is "abgeschlossen" in the UI, so
        # deleting it is a retroactive correction (the lesson never actually
        # happened) rather than a normal cancellation. It gets its own ledger
        # wording — naming who undid it, for a fully transparent audit trail —
        # and sends no cancellation e-mails (there is nothing to call off; the
        # refund shows up in the student's credit history instead).
        retroactive = b.date < timezone.localdate()
        label, sub = "Buchung storniert, Einheit erstattet", None
        if retroactive:
            actor = request.user.get_full_name() or request.user.username
            label = "Stunde rückwirkend storniert, Einheit erstattet"
            sub = f"{b.ledger_sub()} · von {actor}"
        # A tutor/admin removal always returns the credit; a student cancelling
        # inside the 24h window forfeits it (mirrors the booking UI's policy).
        # An unconfirmed request is always refunded — it was never a firm booking.
        refund_txn, credits = _cancel_booking(
            b, forfeit=is_student and not b.is_requested and _booking_within_24h(b),
            label=label, sub=sub, notify=not retroactive,
        )
        return JsonResponse({
            "ok": True, "refunded": refund_txn is not None, "credits": credits,
            "retroactive": retroactive,
            "txn": serialize_transaction(refund_txn) if refund_txn else None,
        })

    # PUT
    data = parse_body(request)
    old_slot = (b.date, b.time)
    if "title" in data:
        b.title = data["title"]
    if "date" in data:
        b.date = date.fromisoformat(data["date"])
    if "time" in data:
        if not _TIME_RE.match(str(data["time"])):
            return JsonResponse({"error": "invalid time"}, status=400)
        b.time = data["time"]
    # A reschedule must land on a free, open slot — same guard as creating one.
    if ("date" in data or "time" in data) and b.tutor_id:
        conflict = _slot_unavailable(b.tutor, b.date, b.time, exclude_pk=b.pk)
        if conflict:
            return JsonResponse({"error": conflict}, status=409)
    # A student moving their own booking needs the tutor's OK again for the new
    # slot — otherwise rescheduling would bypass confirmation.
    re_requested = is_student and (b.date, b.time) != old_slot
    if re_requested:
        b.status = Booking.STATUS_REQUESTED
        b.requested_at = timezone.now()
    if "notes" in data:
        b.notes = data["notes"]
    if "homeworkDone" in data:
        b.homework_done = bool(data["homeworkDone"])
    # tutorNotes and callLink are tutor-owned fields — students can't set them.
    if not is_student:
        if "tutorNotes" in data:
            b.tutor_notes = data["tutorNotes"]
        if "summary" in data:
            b.summary = str(data["summary"] or "")[:5000]
        if "homework" in data:
            b.homework = str(data["homework"] or "")[:5000]
        if "callLink" in data:
            # Rendered as a clickable href, so only web links: a bare
            # "zoom.us/j/…" gets https://, any other scheme (javascript:, data:)
            # is refused.
            link = str(data["callLink"] or "").strip()[:500]
            if link and not re.match(r"^https?://", link, re.I):
                if re.match(r"^[a-z][a-z0-9+.-]*:", link, re.I) and not re.match(r"^[^/:]+:\d", link):
                    return JsonResponse({"error": "Link muss mit http(s):// beginnen."}, status=400)
                link = "https://" + link
            data["callLink"] = link
        if "callLink" in data and data["callLink"] != b.call_link:
            # A hand-edited link supersedes the auto-created meeting: remove
            # the orphan from the tutor's account and drop the reference so a
            # later cancel/reschedule never touches the wrong call.
            if b.video_meeting_id:
                emails.queue_email(
                    video.cleanup_meeting, b.tutor_id, b.video_provider, b.video_meeting_id
                )
            b.video_provider = ""
            b.video_meeting_id = ""
            b.call_link = data["callLink"]
    b.save()
    # A rescheduled booking drags its auto-created Zoom/Teams meeting along to
    # the new slot (best-effort, off-request) so the mailed link stays valid.
    if b.video_meeting_id and (b.date, b.time) != old_slot:
        emails.queue_email(video.move_meeting, b.pk)
    if re_requested:
        emails.queue_email(emails.send_lesson_request_tutor, b.pk)
    return JsonResponse({"ok": True, "status": b.status})


# ---------------------------------------------------------------------------
# Fehler-Training (error cards)
# ---------------------------------------------------------------------------

def _card_text(data, key, required):
    value = str(data.get(key) or "").strip()
    if required and not value:
        raise ValueError(f"{key} required")
    return value[:1000]


MAX_CARD_RECTS = 60
MAX_CARDS_PER_PASTE = 100


def _card_rects(value):
    """Highlight boxes from the worksheet viewer: up to MAX_CARD_RECTS lists of
    four numbers [x, y, w, h], each a fraction of the page size."""
    if not isinstance(value, list) or len(value) > MAX_CARD_RECTS:
        raise ValueError("invalid highlight")
    rects = []
    for r in value:
        if not (isinstance(r, list) and len(r) == 4
                and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in r)):
            raise ValueError("invalid highlight")
        x, y, w, h = (round(float(n), 4) for n in r)
        if not (-0.05 <= x <= 1.05 and -0.05 <= y <= 1.05 and 0 < w <= 1.1 and 0 < h <= 1.1):
            raise ValueError("invalid highlight")
        rects.append([x, y, w, h])
    return rects


@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_error_cards(request):
    """Tutor records cards for a student: a mistake or a new word from a lesson.

    One card per request, optionally anchored to a highlight in one of the
    lesson's worksheets (fileId + page + rects) — or a pasted list as
    ``items: [{front, back, note}]``, all of the same kind."""
    data = parse_body(request)
    try:
        student = User.objects.get(slug=data.get("studentSlug"), role="student")
    except User.DoesNotExist:
        return JsonResponse({"error": "unknown student"}, status=400)
    booking = None
    if data.get("bookingPk"):
        booking = Booking.objects.filter(pk=data["bookingPk"], student=student).first()
        if booking is None:
            return JsonResponse({"error": "unknown lesson"}, status=400)
    kind = data.get("kind") or ErrorCard.KIND_ERROR
    if kind not in (ErrorCard.KIND_ERROR, ErrorCard.KIND_VOCAB):
        return JsonResponse({"error": "unknown kind"}, status=400)
    base = {"kind": kind, "student": student, "booking": booking, "tutor": request.user}

    if "items" in data:
        items = data["items"]
        if not isinstance(items, list) or not 0 < len(items) <= MAX_CARDS_PER_PASTE:
            return JsonResponse({"error": f"1–{MAX_CARDS_PER_PASTE} Einträge pro Liste."}, status=400)
        try:
            cards = [ErrorCard(**base, front=_card_text(it, "front", True), back=_card_text(it, "back", True),
                               note=_card_text(it, "note", False))
                     for it in items if isinstance(it, dict)]
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        if len(cards) != len(items):
            return JsonResponse({"error": "invalid items"}, status=400)
        with db_transaction.atomic():
            for c in cards:
                c.save()
        return JsonResponse({"cards": [serialize_error_card(c) for c in cards]})

    try:
        front = _card_text(data, "front", True)
        back = _card_text(data, "back", True)
        note = _card_text(data, "note", False)
        source_file, page, rects = None, None, []
        if data.get("fileId"):
            source_file = SessionFile.objects.defer("data").filter(
                pk=data["fileId"], booking=booking, kind=SessionFile.KIND_WORKSHEET).first() if booking else None
            if source_file is None:
                raise ValueError("unknown worksheet")
            page = data.get("page")
            if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 2000:
                raise ValueError("invalid page")
            rects = _card_rects(data.get("rects") or [])
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    card = ErrorCard.objects.create(**base, front=front, back=back, note=note,
                                    source_file=source_file, page=page, rects=rects)
    return JsonResponse({"card": serialize_error_card(card)})


@require_http_methods(["PUT", "DELETE"])
@require_roles("tutor", "admin")
def api_error_card_detail(request, pk):
    """Edit a card's text, reopen a mastered card, or delete it."""
    card = ErrorCard.objects.filter(pk=pk).select_related("student").first()
    if not card:
        return JsonResponse({"error": "not found"}, status=404)
    if request.method == "DELETE":
        card.delete()
        return JsonResponse({"ok": True})
    data = parse_body(request)
    try:
        for key in ("front", "back"):
            if key in data:
                setattr(card, key, _card_text(data, key, True))
        if "note" in data:
            card.note = _card_text(data, "note", False)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    if data.get("status") == ErrorCard.STATUS_OPEN:
        card.status, card.mastered_at = ErrorCard.STATUS_OPEN, None
    card.save()
    return JsonResponse({"card": serialize_error_card(card)})


@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_error_card_review(request, pk):
    """Record the result of reviewing a card together in a lesson: "known" marks
    it mastered (done), "again" keeps it in the error log for next time."""
    card = ErrorCard.objects.filter(pk=pk).select_related("student").first()
    if not card:
        return JsonResponse({"error": "not found"}, status=404)
    result = parse_body(request).get("result")
    if result not in ("known", "again"):
        return JsonResponse({"error": "result must be known or again"}, status=400)
    now = timezone.now()
    card.reviews += 1
    card.last_result = result
    card.last_reviewed_at = now
    if result == "known":
        card.status, card.mastered_at = ErrorCard.STATUS_MASTERED, now
    card.save()
    return JsonResponse({"card": serialize_error_card(card)})


# ---------------------------------------------------------------------------
# Session page: exercises and files per lesson
# ---------------------------------------------------------------------------

def _session_booking(request, pk):
    """The lesson behind a session-page request, or None if it doesn't exist or
    the user may not see it (a student only ever sees their own lessons)."""
    b = Booking.objects.filter(pk=pk).select_related("student").first()
    if not b or b.is_intro:
        return None
    if request.user.role == "student" and b.student_id != request.user.pk:
        return None
    return b


@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_session_exercises(request, pk):
    """Tutor adds an exercise to a lesson."""
    b = _session_booking(request, pk)
    if not b:
        return JsonResponse({"error": "not found"}, status=404)
    data = parse_body(request)
    title = str(data.get("title") or "").strip()[:200]
    if not title:
        return JsonResponse({"error": "Titel fehlt."}, status=400)
    link = str(data.get("link") or "").strip()[:500]
    if link and not re.match(r"^https?://", link):
        return JsonResponse({"error": "Link muss mit http(s):// beginnen."}, status=400)
    e = SessionExercise.objects.create(
        booking=b, title=title, description=str(data.get("description") or "")[:3000], link=link,
    )
    return JsonResponse({"exercise": serialize_exercise(e)})


@require_http_methods(["PUT", "DELETE"])
@require_roles("student", "tutor", "admin")
def api_session_exercise_detail(request, ex_id):
    """Tutor edits/deletes an exercise; the student may only tick it done."""
    e = SessionExercise.objects.filter(pk=ex_id).select_related("booking").first()
    if not e or not _session_booking(request, e.booking_id):
        return JsonResponse({"error": "not found"}, status=404)
    is_student = request.user.role == "student"
    if request.method == "DELETE":
        if is_student:
            return JsonResponse({"error": "forbidden"}, status=403)
        e.delete()
        return JsonResponse({"ok": True})
    data = parse_body(request)
    if "done" in data:
        e.done = bool(data["done"])
    if not is_student:
        if "title" in data:
            title = str(data["title"] or "").strip()[:200]
            if not title:
                return JsonResponse({"error": "Titel fehlt."}, status=400)
            e.title = title
        if "description" in data:
            e.description = str(data["description"] or "")[:3000]
        if "link" in data:
            link = str(data["link"] or "").strip()[:500]
            if link and not re.match(r"^https?://", link):
                return JsonResponse({"error": "Link muss mit http(s):// beginnen."}, status=400)
            e.link = link
    e.save()
    return JsonResponse({"exercise": serialize_exercise(e)})


def _session_file_bytes(sf):
    """A lesson file's content: its own bytes, or the Lernmaterialien file it links to."""
    if sf.data is not None:
        return bytes(sf.data)
    return _lesson_file_bytes(sf.material) if sf.material_id else None


@require_http_methods(["GET"])
@require_roles("student", "tutor", "admin")
def api_session_pdf(request, pk):
    """The lesson's overview as a PDF (summary, vocab, mistakes, exercises,
    homework, worksheets) for the tutor to send on, or the student to keep.
    Same access as the lesson page: a student only gets their own lessons."""
    b = _session_booking(request, pk)
    if not b:
        raise Http404
    try:
        pdf = render_lesson_pdf(b, sheet_bytes=_session_file_bytes)
    except Exception:
        logger.exception("lesson pdf failed for booking %s", b.pk)
        return JsonResponse({"error": "pdf_unavailable"}, status=503)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{lesson_pdf_filename(b)}"'
    return resp


@require_http_methods(["POST"])
@require_roles("student", "tutor", "admin")
def api_session_files(request, pk):
    """Upload to a lesson: the tutor adds worksheets, the student hands in homework."""
    b = _session_booking(request, pk)
    if not b:
        return JsonResponse({"error": "not found"}, status=404)
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"error": "Keine Datei hochgeladen."}, status=400)
    name = (f.name or "datei")[:255]
    if file_ext(name) not in ALLOWED_LESSON_EXTS:
        return JsonResponse(
            {"error": "Dateityp nicht unterstützt. Erlaubt: PDF, Office-Dokumente, Bilder, Audio, ZIP."},
            status=400,
        )
    if f.size > MAX_LESSON_FILE_BYTES:
        return JsonResponse({"error": "Datei zu groß (max. 10 MB)."}, status=400)
    kind = SessionFile.KIND_HOMEWORK if request.user.role == "student" else SessionFile.KIND_WORKSHEET
    sf = SessionFile.objects.create(
        booking=b, kind=kind, name=name, size=f.size, data=f.read(),
        content_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
        uploaded_by=request.user,
    )
    return JsonResponse({"file": serialize_session_file(sf)})


@require_http_methods(["GET", "DELETE"])
@require_roles("student", "tutor", "admin")
def api_session_file_detail(request, file_id):
    """Download a lesson file, or delete it (tutor/admin, or the student's own upload)."""
    sf = SessionFile.objects.filter(pk=file_id).first()
    if not sf or not _session_booking(request, sf.booking_id):
        raise Http404
    if request.method == "DELETE":
        if request.user.role == "student" and sf.uploaded_by_id != request.user.pk:
            return JsonResponse({"error": "forbidden"}, status=403)
        sf.delete()
        return JsonResponse({"ok": True})
    data = _session_file_bytes(sf)
    if data is None:
        raise Http404
    return _attachment(data, sf.name, sf.content_type)


@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_session_file_import(request, pk):
    """Put a Lernmaterialien file into a lesson as a worksheet. The lesson only
    links to it, so the file is stored once however many lessons use it."""
    b = _session_booking(request, pk)
    if not b:
        return JsonResponse({"error": "not found"}, status=404)
    lf = LessonFile.objects.defer("data").filter(pk=parse_body(request).get("materialId")).first()
    if not lf:
        return JsonResponse({"error": "Datei nicht gefunden."}, status=404)
    if not LessonFile.objects.filter(pk=lf.pk, data__isnull=False).exists():
        return JsonResponse({"error": "Diese Datei ist nicht mehr verfügbar."}, status=400)
    sf = SessionFile.objects.defer("data").filter(booking=b, material=lf).first()
    if not sf:
        sf = SessionFile.objects.create(
            booking=b, kind=SessionFile.KIND_WORKSHEET, name=lf.original_name, size=lf.size,
            data=None, material=lf, uploaded_by=request.user,
            content_type=lf.content_type or mimetypes.guess_type(lf.original_name)[0] or "application/octet-stream",
        )
    return JsonResponse({"file": serialize_session_file(sf)})


# ---------------------------------------------------------------------------
# Einheiten API
# ---------------------------------------------------------------------------

# How a tutor-recorded top-up was paid (card payments go through the student's
# own Stripe checkout instead).
TOPUP_METHODS = {"cash": "bar bezahlt", "transfer": "per Überweisung bezahlt"}
MAX_TOPUP_CREDITS = 500
MAX_TOPUP_TOTAL_CENTS = 50_000_00


def parse_euro_cents(value):
    """Euro amount ("355", "355,50", 355.5) -> integer cents, or None if invalid,
    negative or with more than two decimals."""
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
        return None
    return int(amount * 100)


@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_credits(request, slug):
    """Tutor/admin adds credits and issues the receipt. Standard top-ups use pack
    pricing; a custom deal passes ``total`` (euros) for an exact receipt total,
    plus an optional payment ``method`` and ``note`` shown in the history."""
    data = parse_body(request)
    try:
        student = User.objects.get(slug=slug, role="student")
        n = int(data.get("n", 1))
        if n <= 0 or n > MAX_TOPUP_CREDITS:
            raise ValueError(f"Anzahl muss zwischen 1 und {MAX_TOPUP_CREDITS} liegen.")
    except (User.DoesNotExist, ValueError, TypeError) as e:
        return JsonResponse({"error": str(e)}, status=400)

    total_cents = None
    if data.get("total") not in (None, ""):
        total_cents = parse_euro_cents(data["total"])
        if total_cents is None or total_cents > MAX_TOPUP_TOTAL_CENTS:
            return JsonResponse({"error": "Ungültiger Betrag."}, status=400)
    method = data.get("method") or "cash"
    if method not in TOPUP_METHODS:
        return JsonResponse({"error": "Ungültige Zahlungsart."}, status=400)
    note = (data.get("note") or "").strip()[:80]

    receipt = grant_credits(
        student, n, get_settings(),
        label=note or "Einheiten vom Tutor",
        sub=f"Heute · {TOPUP_METHODS[method]}",
        total_cents=total_cents,
    )
    return JsonResponse({"receipt": serialize_receipt(receipt)})


def reverse_opening_credit(txn):
    """Reverse a one-time opening balance (e.g. a fat-finger correction). Deducts the
    granted credits back off the student and records a reversing, receipt-less
    ``open`` ledger entry; marks the original cancelled so a fresh opening balance
    can be booked again. Returns the reversing transaction, or None if already
    cancelled (a concurrent caller won)."""
    with db_transaction.atomic():
        locked = CreditTransaction.objects.select_for_update().get(pk=txn.pk)
        if locked.cancelled:
            return None
        n = locked.amount
        student, student_slug, student_name = _reverse_credits(locked)
        rev = CreditTransaction.objects.create(
            student=student,
            student_slug=student_slug,
            student_name=student_name,
            txn_type="open",
            label="Eröffnungsguthaben storniert",
            sub="Rückgängig gemacht",
            amount=-n,
            reverses=locked,
        )
        locked.cancelled = True
        locked.save(update_fields=["cancelled"])
    return rev


@require_http_methods(["POST"])
@require_roles("admin")
def api_cancel_transaction(request, txn_id):
    """Admin-only: reverse a credit movement.

    A purchase (``buy`` with a receipt) is reversed via a Storno credit note + Stripe
    refund; an opening balance (``open``) is reversed with a plain receipt-less
    ledger entry. Either way the credits are undone, the original is marked
    cancelled, and both the student and the admin see the reversal.
    """
    txn = CreditTransaction.objects.filter(pk=txn_id).first()
    if not txn:
        return JsonResponse({"error": "not found"}, status=404)
    if txn.cancelled:
        return JsonResponse({"error": "not_cancellable"}, status=400)

    # Opening balance → simple receipt-less reversal.
    if txn.txn_type == "open" and txn.reverses_id is None and txn.amount > 0:
        rev = reverse_opening_credit(txn)
        if not rev:
            return JsonResponse({"error": "not_cancellable"}, status=400)
        student = rev.student
        return JsonResponse({
            "ok": True,
            "credits": student.credits if student is not None else None,
            "stornoTxn": serialize_transaction(rev),
            "cancelledTxnId": txn.pk,
            "receipt": None,
        })

    if txn.txn_type != "buy" or not txn.receipt_no:
        return JsonResponse({"error": "not_cancellable"}, status=400)

    result = cancel_purchase(txn)
    if not result:
        # Lost the race (already cancelled) or the original receipt is missing.
        return JsonResponse({"error": "not_cancellable"}, status=400)
    storno_receipt, storno_txn = result

    original = storno_receipt.reverses
    refunded_stripe = bool(refund_stripe_purchase(original, storno_receipt)) if original else False

    # Notify the student and the business once the reversal has committed.
    db_transaction.on_commit(
        lambda: emails.queue_email(emails.send_storno_notifications, storno_receipt.pk)
    )

    student = storno_txn.student
    return JsonResponse({
        "ok": True,
        "refundedStripe": refunded_stripe,
        "credits": student.credits if student is not None else None,
        "stornoTxn": serialize_transaction(storno_txn),
        "cancelledTxnId": txn.pk,
        "receiptNo": storno_receipt.number,
        "receipt": serialize_receipt(storno_receipt),
    })


# ---------------------------------------------------------------------------
# Billing API
# ---------------------------------------------------------------------------

def _apply_name(u, name):
    """Set the display name everywhere it's derived: first/last, billing, initials."""
    name = name or ""
    u.first_name, _, u.last_name = name.strip().partition(" ")
    u.billing_name = name
    u.initials = compute_initials(name)


def _apply_billing(u, data):
    for field in ("line1", "postcode", "city"):
        if field in data:
            setattr(u, f"billing_{field}", data[field])
    if "country" in data:
        u.billing_country = data["country"] or "Österreich"


@require_http_methods(["PUT"])
@require_roles("student", "tutor", "admin")
def api_billing(request):
    data = parse_body(request)
    if "email" in data:
        new_email = (data["email"] or "").strip().lower()
        try:
            validate_email(new_email)
        except ValidationError:
            return JsonResponse({"error": "Bitte eine gültige E-Mail-Adresse eingeben."}, status=400)
        # Email is the login identifier — keep it unique.
        if User.objects.filter(email__iexact=new_email).exclude(pk=request.user.pk).exists():
            return JsonResponse({"error": "Diese E-Mail wird bereits verwendet."}, status=400)
        request.user.email = new_email
    if "name" in data:
        _apply_name(request.user, data["name"])
    _apply_billing(request.user, data)
    request.user.save()
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Stripe checkout API (self-service credit top-ups)
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("student")
def api_checkout(request):
    """Create a Stripe Checkout session for the current student to buy ``n``
    credits, and return its hosted-payment URL. Price is computed server-side."""
    if not stripe_enabled():
        return JsonResponse({"error": "stripe_disabled"}, status=503)
    data = parse_body(request)
    try:
        n = int(data.get("n", 1))
        if n <= 0:
            raise ValueError("n must be positive")
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid amount"}, status=400)

    # A card payment needs a completed account: a real e-mail for the receipts
    # and a receipt address.
    missing = payment_setup_missing(request.user)
    if missing:
        return setup_required_response(missing)

    settings = get_settings()
    amount = pack_price_cents(settings, n)
    origin = request.build_absolute_uri("/").rstrip("/")
    client = stripe_client()
    try:
        session = client.checkout.Session.create(
            mode="payment",
            line_items=[{
                "quantity": 1,
                "price_data": {
                    "currency": "eur",
                    "unit_amount": amount,
                    "product_data": {
                        "name": f"{n} Einheiten · the green pencil",
                        "description": "1 Einheit = 45 Minuten Englisch-Einzelunterricht",
                    },
                },
            }],
            # Server-trusted facts the webhook/redirect use to credit the right
            # student. The price is set above, not taken from the client.
            metadata={"student_slug": request.user.slug, "credits": str(n)},
            client_reference_id=request.user.slug,
            customer_email=emails._account_email(request.user) or None,
            success_url=f"{origin}/app/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/app/?checkout=cancel",
        )
    except Exception:
        # Don't leak Stripe internals to the client; the UI falls back to e-mail.
        return JsonResponse({"error": "stripe_error"}, status=502)
    return JsonResponse({"url": session.url, "id": session.id})


@require_http_methods(["POST"])
@require_roles("student")
def api_checkout_confirm(request):
    """Called when the student returns from Stripe. Verifies the session was paid
    and credits them if a webhook hasn't already. Idempotent."""
    if not stripe_enabled():
        return JsonResponse({"error": "stripe_disabled"}, status=503)
    data = parse_body(request)
    sid = (data.get("sessionId") or "").strip()
    if not sid:
        return JsonResponse({"error": "missing session"}, status=400)
    client = stripe_client()
    try:
        session = client.checkout.Session.retrieve(sid)
    except Exception:
        return JsonResponse({"error": "not found"}, status=404)
    # A student may only confirm a session that was created for them.
    meta = session.get("metadata") or {}
    if meta.get("student_slug") != request.user.slug:
        return JsonResponse({"error": "forbidden"}, status=403)
    receipt = credit_from_stripe_session(session)
    if not receipt:
        return JsonResponse({"paid": False})
    request.user.refresh_from_db()
    return JsonResponse({
        "paid": True,
        "credits": request.user.credits,
        "receipt": serialize_receipt(receipt),
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_stripe_webhook(request):
    """Stripe -> us. Verifies the signature (when a webhook secret is configured)
    and credits the student on checkout.session.completed. The source of truth for
    crediting; the post-payment redirect is only a faster-feeling fallback."""
    secret = dj_settings.STRIPE_WEBHOOK_SECRET
    # Without a signing secret anyone could post a "paid" event and mint credits,
    # so the endpoint stays off until one is configured (`stripe listen` prints
    # one for local dev).
    if not stripe_enabled() or not secret:
        return JsonResponse({"error": "stripe_disabled"}, status=503)
    try:
        # Raises on a bad payload or a signature that doesn't match the secret
        # (i.e. a forged event). Exception type varies across SDK versions, so
        # catch broadly here — this block only constructs the event.
        event = stripe_client().Webhook.construct_event(
            request.body, request.META.get("HTTP_STRIPE_SIGNATURE", ""), secret,
        )
    except Exception:
        return JsonResponse({"error": "invalid"}, status=400)
    if event.get("type") == "checkout.session.completed":
        credit_from_stripe_session(event["data"]["object"])
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Settle outstanding (negative) credits via Stripe
# ---------------------------------------------------------------------------

def _create_settle_checkout(request, student, n, settings, *, success_path, cancel_path):
    """Create a Stripe Checkout session that charges ``student`` for ``n`` credits
    at the tier settlement rate, tagged so the webhook/redirect credit them back to
    zero. Returns the hosted-payment URL, or None on a Stripe error."""
    unit_euros = settle_unit_euros(settings, n)
    origin = request.build_absolute_uri("/").rstrip("/")
    client = stripe_client()
    try:
        session = client.checkout.Session.create(
            mode="payment",
            line_items=[{
                "quantity": n,
                "price_data": {
                    "currency": "eur",
                    "unit_amount": unit_euros * 100,
                    "product_data": {
                        "name": f"{n} offene Einheiten · the green pencil",
                        "description": "Begleichung offener Einheiten · 1 Einheit = 45 Minuten",
                    },
                },
            }],
            # Server-trusted facts: who to credit, how many, the settlement unit
            # price (so the receipt matches), and the flow kind.
            metadata={
                "student_slug": student.slug, "credits": str(n),
                "kind": "settle", "unit": str(unit_euros),
            },
            client_reference_id=student.slug,
            customer_email=emails._account_email(student) or None,
            success_url=f"{origin}{success_path}",
            cancel_url=f"{origin}{cancel_path}",
        )
    except Exception:
        return None
    return session.url


@require_http_methods(["POST"])
@require_roles("student")
def api_settle(request):
    """Student self-service: pay off one's own negative balance. Charges exactly the
    outstanding amount so the balance returns to zero."""
    if not stripe_enabled():
        return JsonResponse({"error": "stripe_disabled"}, status=503)
    outstanding = max(0, -request.user.credits)
    if outstanding <= 0:
        return JsonResponse({"error": "nothing_outstanding"}, status=400)
    # Settling is a card payment too — the account must be set up first.
    missing = payment_setup_missing(request.user)
    if missing:
        return setup_required_response(missing)
    settings = get_settings()
    url = _create_settle_checkout(
        request, request.user, outstanding, settings,
        success_path="/app/?checkout=success&session_id={CHECKOUT_SESSION_ID}",
        cancel_path="/app/?checkout=cancel",
    )
    if not url:
        return JsonResponse({"error": "stripe_error"}, status=502)
    return JsonResponse({"url": url})


@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_settle_link(request, slug):
    """Tutor/admin: mint (or reuse) the distributable settlement link for a student
    who owes credits. The student can open it without logging in and pay."""
    student = User.objects.filter(slug=slug, role="student").first()
    if not student:
        return JsonResponse({"error": "not found"}, status=404)
    outstanding = max(0, -student.credits)
    if outstanding <= 0:
        return JsonResponse({"error": "nothing_outstanding"}, status=400)
    missing = payment_setup_missing(student)
    if missing:
        return setup_required_response(missing)
    if not student.settle_token:
        student.settle_token = secrets.token_urlsafe(24)
        student.save(update_fields=["settle_token"])
    origin = request.build_absolute_uri("/").rstrip("/")
    settings = get_settings()
    return JsonResponse({
        "url": f"{origin}/settle/{student.settle_token}/",
        "outstanding": outstanding,
        "amount": settle_unit_euros(settings, outstanding) * outstanding,
    })


def _settle_student_for_token(token):
    return (
        User.objects.filter(settle_token=token, role="student").first()
        if token else None
    )


def settle_page(request, token):
    """Public capability-URL page where a student settles outstanding credits. No
    login required — possession of the unguessable token authorizes payment."""
    if request.GET.get("paid") == "1":
        # Stripe's return URL. The webhook may already have retired the token.
        return render(request, "settle.html", {"paid": True})
    student = _settle_student_for_token(token)
    if not student:
        return render(request, "settle.html", {"invalid": True}, status=404)
    outstanding = max(0, -student.credits)
    settings = get_settings()
    return render(request, "settle.html", {
        "invalid": False,
        "token": token,
        "first_name": student.first_name or student.username,
        "outstanding": outstanding,
        "unit": settle_unit_euros(settings, outstanding) if outstanding else 0,
        "amount": settle_unit_euros(settings, outstanding) * outstanding,
        "stripe_enabled": stripe_enabled(),
        "setup_missing": payment_setup_missing(student),
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_settle_token_checkout(request, token):
    """Public: create the Checkout session for a token's current outstanding amount.
    CSRF-exempt because the URL token is the capability (worst case a stranger pays
    someone's debt — harmless) and the page may be opened without a session."""
    if not stripe_enabled():
        return JsonResponse({"error": "stripe_disabled"}, status=503)
    student = _settle_student_for_token(token)
    if not student:
        return JsonResponse({"error": "not found"}, status=404)
    outstanding = max(0, -student.credits)
    if outstanding <= 0:
        return JsonResponse({"error": "nothing_outstanding"}, status=400)
    missing = payment_setup_missing(student)
    if missing:
        return setup_required_response(missing)
    settings = get_settings()
    url = _create_settle_checkout(
        request, student, outstanding, settings,
        success_path=f"/settle/{token}/?paid=1",
        cancel_path=f"/settle/{token}/",
    )
    if not url:
        return JsonResponse({"error": "stripe_error"}, status=502)
    return JsonResponse({"url": url})


# ---------------------------------------------------------------------------
# Availability API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_availability(request):
    data = parse_body(request)
    try:
        d = date.fromisoformat(data["date"])
        time_str = data["time"]
        is_open = bool(data.get("isOpen", True))
        tutor = acting_tutor(request, data.get("tutorSlug"))
        if tutor is None:
            return JsonResponse({"error": "unknown tutor"}, status=400)
        AvailabilityOverride.objects.update_or_create(
            tutor=tutor,
            date=d,
            time=time_str,
            defaults={"is_open": is_open},
        )
        return JsonResponse({"ok": True})
    except (KeyError, ValueError, TypeError) as e:
        return JsonResponse({"error": str(e)}, status=400)


# ---------------------------------------------------------------------------
# Custom Times API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_custom_times(request):
    data = parse_body(request)
    try:
        d = date.fromisoformat(data["date"])
        time_str = data["time"]
        tutor = acting_tutor(request, data.get("tutorSlug"))
        if tutor is None:
            return JsonResponse({"error": "unknown tutor"}, status=400)
        CustomTime.objects.get_or_create(tutor=tutor, date=d, time=time_str)
        return JsonResponse({"ok": True})
    except (KeyError, ValueError, TypeError) as e:
        return JsonResponse({"error": str(e)}, status=400)


# ---------------------------------------------------------------------------
# Opening balance (migration)
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("admin")
def api_opening_credit(request, slug):
    """Admin-only: set a student's one-time opening balance (Eröffnungsguthaben).

    For students migrating onto the platform with credits they already hold from
    outside it. It replaces the old free-form credit reset (which changed a balance
    silently, with no trail — a fraud risk). The opening balance is booked as an
    audited ledger transaction that the student can see; it carries no receipt,
    because the credits were paid for elsewhere and issuing one would invent
    revenue. Allowed once per student.
    """
    student = User.objects.filter(slug=slug, role="student").first()
    if not student:
        return JsonResponse({"error": "not found"}, status=404)
    data = parse_body(request)
    try:
        n = int(data.get("n"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "invalid amount"}, status=400)
    if n == 0:
        return JsonResponse({"error": "invalid amount"}, status=400)
    note = (data.get("note") or "").strip()[:200]

    with db_transaction.atomic():
        locked = User.objects.select_for_update().get(pk=student.pk)
        # One active opening balance per student. A reversed one (cancelled) frees
        # the slot so a corrected balance can be booked after a fat-finger.
        if CreditTransaction.objects.filter(
            student=locked, txn_type="open", cancelled=False, amount__gt=0
        ).exists():
            return JsonResponse({"error": "already_set"}, status=409)
        locked.credits += n
        locked.save(update_fields=["credits"])
        txn = CreditTransaction.log(
            locked,
            txn_type="open",
            label="Eröffnungsguthaben",
            sub=note or "Übertrag bestehender Einheiten",
            amount=n,
        )
    return JsonResponse({"ok": True, "credits": locked.credits, "txn": serialize_transaction(txn)})


# ---------------------------------------------------------------------------
# Notes API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_notes(request, slug):
    data = parse_body(request)
    try:
        student = User.objects.get(slug=slug, role="student")
        text = (data.get("text") or "").strip()
        if not text:
            return JsonResponse({"error": "text required"}, status=400)
        # Attribute to the acting tutor — the one the client is working as (which,
        # under admin "view-as", is the impersonated tutor), not just the first one.
        tutor = acting_tutor(request, data.get("tutorSlug"))
        note = StudentNote.objects.create(tutor=tutor, student=student, text=text)
        return JsonResponse({"ok": True, "date": note.created_at.strftime("%d.%m.%Y")})
    except User.DoesNotExist as e:
        return JsonResponse({"error": str(e)}, status=404)


# ---------------------------------------------------------------------------
# Schularbeiten / tests API
# ---------------------------------------------------------------------------

SCHOOL_TEST_KINDS = {c for c, _ in SchoolTest.KIND_CHOICES}


def _school_test_fields(data, test):
    """Apply the editable fields present in ``data`` to ``test``; ValueError
    names the first invalid one."""
    if "kind" in data:
        if data["kind"] not in SCHOOL_TEST_KINDS:
            raise ValueError("unknown kind")
        test.kind = data["kind"]
    if "date" in data:
        try:
            test.date = date.fromisoformat(str(data["date"]))
        except ValueError:
            raise ValueError("invalid date")
    if "grade" in data:
        g = data["grade"]
        if g in (None, ""):
            test.grade = None
        elif isinstance(g, int) and not isinstance(g, bool) and 1 <= g <= 5:
            test.grade = g
        else:
            raise ValueError("grade must be 1–5")
    for key, attr, limit in (("topic", "topic", 200), ("wentWell", "went_well", 2000),
                             ("toImprove", "to_improve", 2000)):
        if key in data:
            setattr(test, attr, str(data[key] or "").strip()[:limit])


@require_http_methods(["POST"])
@require_roles("student", "tutor", "admin")
def api_school_tests(request):
    """Add a Schularbeit/test. A student adds their own; tutor/admin name the
    student by ``studentSlug``."""
    data = parse_body(request)
    if request.user.role == "student":
        student = request.user
    else:
        student = User.objects.filter(slug=data.get("studentSlug"), role="student").first()
        if student is None:
            return JsonResponse({"error": "unknown student"}, status=400)
    if not data.get("date"):
        return JsonResponse({"error": "date required"}, status=400)
    test = SchoolTest(student=student, created_by=request.user)
    try:
        _school_test_fields(data, test)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    test.save()
    return JsonResponse({"test": serialize_school_test(test)})


@require_http_methods(["PUT", "DELETE"])
@require_roles("student", "tutor", "admin")
def api_school_test_detail(request, pk):
    qs = SchoolTest.objects.select_related("student")
    if request.user.role == "student":
        qs = qs.filter(student=request.user)
    test = qs.filter(pk=pk).first()
    if test is None:
        return JsonResponse({"error": "not found"}, status=404)
    if request.method == "DELETE":
        test.delete()
        return JsonResponse({"ok": True})
    try:
        _school_test_fields(parse_body(request), test)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    test.save()
    return JsonResponse({"test": serialize_school_test(test)})


# ---------------------------------------------------------------------------
# Lessons API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_lessons(request, slug):
    data = parse_body(request)
    try:
        student = User.objects.get(slug=slug, role="student")
        lesson_id = data.get("lessonId", "")
        if data.get("on"):
            ActiveLesson.objects.get_or_create(student=student, lesson_id=lesson_id)
        else:
            ActiveLesson.objects.filter(student=student, lesson_id=lesson_id).delete()
        return JsonResponse({"ok": True})
    except User.DoesNotExist as e:
        return JsonResponse({"error": str(e)}, status=404)


# ---------------------------------------------------------------------------
# Topics API (the Lernmaterialien library: level × skill)
# ---------------------------------------------------------------------------

TOPIC_LEVELS = {c for c, _ in CurriculumTopic.LEVEL_CHOICES}
TOPIC_SKILLS = {c for c, _ in CurriculumTopic.SKILL_CHOICES}


def _topic_fields(data, topic=None):
    """Validated level/skill/title from a request body; on edit, missing keys
    keep the topic's current value. Returns (fields, error)."""
    level = data.get("level", topic.level if topic else "")
    skill = data.get("skill", topic.skill if topic else "")
    title = (data.get("title", topic.title if topic else "") or "").strip()
    if level not in TOPIC_LEVELS:
        return None, "Unbekanntes Niveau."
    if skill not in TOPIC_SKILLS:
        return None, "Unbekannter Bereich."
    if not title:
        return None, "Bitte einen Titel eingeben."
    return {"level": level, "skill": skill, "title": title[:200]}, None


@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_topics(request):
    fields, err = _topic_fields(parse_body(request))
    if err:
        return JsonResponse({"error": err}, status=400)
    last = CurriculumTopic.objects.order_by("-position").first()
    while True:
        lesson_id = f"{fields['level'].lower()}-{secrets.token_hex(4)}"
        if not CurriculumTopic.objects.filter(lesson_id=lesson_id).exists():
            break
    t = CurriculumTopic.objects.create(
        lesson_id=lesson_id, position=(last.position + 1) if last else 0, **fields
    )
    return JsonResponse(serialize_topic(t))


@require_http_methods(["PATCH", "DELETE"])
@require_roles("tutor", "admin")
def api_topic_detail(request, lesson_id):
    try:
        t = CurriculumTopic.objects.get(lesson_id=lesson_id)
    except CurriculumTopic.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)
    if request.method == "DELETE":
        # The topic's files and every student's unlock go with it.
        for lf in LessonFile.objects.filter(lesson_id=lesson_id):
            _delete_lesson_file(lf)
        ActiveLesson.objects.filter(lesson_id=lesson_id).delete()
        t.delete()
        return JsonResponse({"ok": True})
    fields, err = _topic_fields(parse_body(request), t)
    if err:
        return JsonResponse({"error": err}, status=400)
    for k, v in fields.items():
        setattr(t, k, v)
    t.save()
    return JsonResponse(serialize_topic(t))


# ---------------------------------------------------------------------------
# Lesson files API (PDF materials, shared per lesson)
# ---------------------------------------------------------------------------

def _lesson_file_bytes(lf):
    """A Lernmaterialien file's content, or None if it is gone."""
    if lf.data is not None:
        return bytes(lf.data)
    if lf.file:  # legacy upload on the web server's disk (gone after a redeploy)
        try:
            with lf.file.open("rb") as fh:
                return fh.read()
        except (FileNotFoundError, ValueError):
            return None
    return None


def _delete_lesson_file(lf):
    """Delete a Lernmaterialien file. Lessons it was linked into get their own
    copy first, so removing it from the library never empties a lesson."""
    links = SessionFile.objects.defer("data").filter(material=lf, data__isnull=True)
    if links.exists():
        data = _lesson_file_bytes(lf)
        if data is not None:
            for sf in links:
                sf.data = data
                sf.save(update_fields=["data"])
    if lf.file:
        lf.file.delete(save=False)  # legacy on-disk upload
    lf.delete()


@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_lesson_files(request, lesson_id):
    if not CurriculumTopic.objects.filter(lesson_id=lesson_id).exists():
        return JsonResponse({"error": "Unbekanntes Thema."}, status=404)
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"error": "Keine Datei hochgeladen."}, status=400)
    name = f.name or "file"
    if file_ext(name) not in ALLOWED_LESSON_EXTS:
        return JsonResponse(
            {"error": "Dateityp nicht unterstützt. Erlaubt: PDF, Office-Dokumente, Bilder, Audio, ZIP."},
            status=400,
        )
    if f.size > MAX_LESSON_FILE_BYTES:
        return JsonResponse({"error": "Datei zu groß (max. 10 MB)."}, status=400)
    lf = LessonFile.objects.create(
        lesson_id=lesson_id, original_name=name[:255], uploaded_by=request.user,
        data=f.read(), size=f.size,
        content_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
    )
    return JsonResponse(serialize_lesson_file(lf))


@require_http_methods(["DELETE"])
@require_roles("tutor", "admin")
def api_lesson_file_detail(request, file_id):
    try:
        lf = LessonFile.objects.get(pk=file_id)
    except LessonFile.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)
    _delete_lesson_file(lf)
    return JsonResponse({"ok": True})


@require_http_methods(["GET"])
@require_roles("student", "tutor", "admin")
def api_lesson_file_download(request, file_id):
    # Access-controlled file serving (no public MEDIA URL): students may only
    # download materials for lessons they have unlocked; tutor/admin always.
    try:
        lf = LessonFile.objects.get(pk=file_id)
    except LessonFile.DoesNotExist:
        raise Http404
    if request.user.role == "student" and not ActiveLesson.objects.filter(
        student=request.user, lesson_id=lf.lesson_id
    ).exists():
        return JsonResponse({"error": "forbidden"}, status=403)
    if lf.data is not None:
        return _attachment(bytes(lf.data), lf.original_name, lf.content_type)
    # Legacy upload on the web server's disk (gone after a redeploy).
    ctype = mimetypes.guess_type(lf.original_name)[0] or "application/octet-stream"
    try:
        resp = FileResponse(lf.file.open("rb"), content_type=ctype)
    except (FileNotFoundError, ValueError):
        raise Http404
    resp["Content-Disposition"] = f'attachment; filename="{_safe_filename(lf.original_name)}"'
    return resp


def _safe_filename(name):
    """User-supplied filename, sanitized for a Content-Disposition header."""
    return (name or "").replace('"', "").replace("\r", "").replace("\n", "") or "datei"


def _attachment(data, name, content_type):
    """Serve stored bytes as a download. Always an attachment (+ global nosniff)
    so nothing user-uploaded renders inline."""
    resp = HttpResponse(data, content_type=content_type or "application/octet-stream")
    resp["Content-Disposition"] = f'attachment; filename="{_safe_filename(name)}"'
    return resp


# ---------------------------------------------------------------------------
# Receipt PDF
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
@require_roles("student", "tutor", "admin")
def api_receipt_pdf(request, number):
    """Serve an issued receipt (or Storno credit note) as a formatted PDF.

    Access is default-deny: the studio staff (tutor/admin) who manage billing may
    fetch any receipt; everyone else may only fetch a receipt that belongs to them.
    A logged-in student therefore can't open another student's receipt by guessing
    its number. Rendered on the fly from the frozen snapshot, so it reads the same
    forever and needs no stored file."""
    receipt = Receipt.objects.filter(number=number).select_related("student", "reverses").first()
    if not receipt:
        raise Http404
    is_staff = request.user.role in ("tutor", "admin")
    owns_it = receipt.student_id is not None and receipt.student_id == request.user.pk
    if not (is_staff or owns_it):
        return JsonResponse({"error": "forbidden"}, status=403)
    try:
        pdf = render_receipt_pdf(receipt)
    except Exception:
        # reportlab missing or a render error — don't 500 the client.
        return JsonResponse({"error": "pdf_unavailable"}, status=503)
    kind = "Storno" if receipt.reverses_id else "Beleg"
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{kind}-{receipt.number}.pdf"'
    return resp


# ---------------------------------------------------------------------------
# Users API (admin)
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
@require_roles("admin")
def api_users(request):
    if request.method == "GET":
        users = list(User.objects.all())
        return JsonResponse({"users": [serialize_user(u) for u in users]})

    # POST: create a student (default) or a tutor
    data = parse_body(request)
    role = data.get("role", "student")
    if role not in ("student", "tutor"):
        return JsonResponse({"error": "invalid role"}, status=400)

    # Unique, collision-safe slug. The timestamp tail is near-unique, but two
    # rapid creates could clash — loop until the slug is actually free.
    prefix = "tut" if role == "tutor" else "stu"
    base = prefix + str(int(time.time() * 1000))[-8:]
    slug = base
    n = 2
    while User.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1

    name = (data.get("name") or "").strip()
    first_name, _, last_name = name.partition(" ")
    initials, color1, color2, last_default = {
        "tutor": ("NT", "#309050", "#277a42", "Tutor"),
        "student": ("NS", "#52a86a", "#2f8a4d", "Student"),
    }[role]
    # Each account gets a unique, unguessable temporary password — never a shared
    # default. It is returned once (below) so the admin can hand it over; it is not
    # stored in clear text and can't be read back afterwards.
    temp_password = secrets.token_urlsafe(9)
    new_user = User.objects.create_user(
        username=slug, email=f"{slug}@fluent.at", password=temp_password,
        role=role, slug=slug,
        initials=compute_initials(name) if name else initials,
        color1=color1, color2=color2,
        first_name=first_name or "New", last_name=last_name or last_default,
        must_change_password=True,  # temp password: force an own one on first login
    )
    payload = serialize_user(new_user)
    payload["tempPassword"] = temp_password  # shown to the admin once, then discarded
    return JsonResponse(payload)


@require_http_methods(["PUT", "DELETE"])
@require_roles("admin")
def api_user_detail(request, slug):
    try:
        u = User.objects.get(slug=slug)
    except User.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    if request.method == "DELETE":
        # Don't let an admin delete their own account out from under themselves.
        if u == request.user:
            return JsonResponse({"error": "Du kannst dein eigenes Konto nicht löschen."}, status=400)
        # GDPR "anonymise & keep": the account (and its PII — email, photo, notes)
        # is erased, but the financial/lesson history is statutorily retained. The
        # history models use on_delete=SET_NULL, so deleting the user detaches the
        # FK while leaving the rows — and their frozen identity/billing snapshots —
        # untouched. We finalise any missing snapshot first so nothing detaches to
        # an unidentifiable record.
        finalize_history_snapshots(u)
        u.delete()
        return JsonResponse({"ok": True})

    # PUT
    data = parse_body(request)
    if "name" in data:
        _apply_name(u, data["name"])
    if "email" in data:
        new_email = (data["email"] or "").strip().lower()
        # Email is the login identifier — keep it unique to avoid ambiguous logins.
        if new_email and User.objects.filter(email__iexact=new_email).exclude(pk=u.pk).exists():
            return JsonResponse({"error": "Diese E-Mail wird bereits verwendet."}, status=400)
        u.email = new_email
    if "password" in data and data["password"]:
        u.set_password(data["password"])
        # An admin-set password is a handed-over secret, exactly like the temp
        # password at account creation — the user must replace it on next
        # login. Except when the admin edits their *own* account: that IS a
        # self-chosen password, and their live session must survive the change.
        if u == request.user:
            update_session_auth_hash(request, u)
        else:
            u.must_change_password = True
    if "photo" in data:
        # Profile photo as a base64 data URL; null/empty removes it. Persisting
        # here (rather than only in client state) is what makes an admin-set
        # photo visible when the student later signs in on another device. It
        # lands in a CSS url('…'), so only a plain base64 image data URL passes.
        if data["photo"] and not _PHOTO_RE.match(str(data["photo"])):
            return JsonResponse({"error": "Ungültiges Foto."}, status=400)
        u.photo = data["photo"] or None
    # NB: credits are intentionally NOT settable here. A free-form reset left no
    # audit trail (a fraud risk); balances now only move through booked
    # transactions — purchases, bookings, and the one-time opening balance
    # (api_opening_credit).
    if isinstance(data.get("billing"), dict):
        _apply_billing(u, data["billing"])
    u.save()
    return JsonResponse(serialize_user(u))


_AVATAR_BG_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@require_http_methods(["PUT"])
@require_roles("student", "tutor", "admin")
def api_user_avatar(request, slug):
    """Set an emoji + background-colour avatar. Students pick their own; tutors
    and admins can set it for a student (admins for anyone). An empty emoji
    resets to the initials avatar."""
    try:
        u = User.objects.get(slug=slug)
    except User.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)
    me = request.user
    allowed = (u == me or me.role == "admin"
               or (me.role == "tutor" and u.role == "student"))
    if not allowed:
        return JsonResponse({"error": "forbidden"}, status=403)

    data = parse_body(request)
    emoji = str(data.get("emoji") or "").strip()
    bg = str(data.get("bg") or "").strip()
    if emoji:
        # Only non-ASCII (emoji) characters: the value is shown as avatar text,
        # so this keeps it from turning into a free-form nickname.
        if len(emoji) > 16 or any(ord(ch) < 128 for ch in emoji):
            return JsonResponse({"error": "Ungültiges Emoji."}, status=400)
        if not _AVATAR_BG_RE.match(bg):
            return JsonResponse({"error": "Ungültige Farbe."}, status=400)
        u.avatar_emoji, u.avatar_bg = emoji, bg.lower()
        # The emoji is the new profile picture, so it replaces a photo.
        u.photo = None
    else:
        u.avatar_emoji, u.avatar_bg = "", ""
    u.save(update_fields=["avatar_emoji", "avatar_bg", "photo"])
    return JsonResponse(serialize_user(u))


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "PUT"])
@require_roles("admin")
def api_settings(request):
    settings = get_settings()

    if request.method == "GET":
        return JsonResponse({
            "creditPrice": settings.credit_price,
            "packs": json.loads(settings.packs_json),
            "popularN": settings.popular_n,
        })

    # PUT
    data = parse_body(request)
    if "creditPrice" in data:
        try:
            v = int(data["creditPrice"])
            if v > 0:
                settings.credit_price = v
        except (ValueError, TypeError):
            pass
    if "packs" in data:
        # popular_n is the single source of truth for the badge, so strip any
        # per-pack feat/tag flags before persisting to keep packs_json clean.
        cleaned = []
        for p in data["packs"]:
            if isinstance(p, dict):
                p = {k: v for k, v in p.items() if k not in ("feat", "tag")}
            cleaned.append(p)
        settings.packs_json = json.dumps(cleaned)
    if "popularN" in data:
        try:
            settings.popular_n = int(data["popularN"])
        except (ValueError, TypeError):
            pass
    settings.save()
    return JsonResponse({"ok": True})

# ---------------------------------------------------------------------------
# Video calls: Zoom / Microsoft Teams account connection (OAuth)
# ---------------------------------------------------------------------------

# Session key holding the pending OAuth handshake: {"state": ..., "provider": ...}.
# The state is the CSRF guard for the callback — it must round-trip untouched.
VIDEO_OAUTH_SESSION_KEY = "video_oauth"


def _video_redirect_uri(request, provider):
    # Derived from the request so dev (localhost) and prod both work; must be
    # byte-identical between the authorize redirect and the code exchange, and
    # registered as-is in the provider's OAuth app.
    return request.build_absolute_uri(f"/oauth/video/{provider}/callback/")


@require_http_methods(["GET"])
def video_oauth_start(request, provider):
    """Send the logged-in tutor to Zoom/Microsoft to grant access.

    Browser navigation rather than a fetch (OAuth is a redirect dance), so
    every failure lands back in the app with a ?video=… flag the UI toasts."""
    if not request.user.is_authenticated or request.user.role != "tutor":
        return redirect("/app/")
    if provider not in video.PROVIDERS or not video.provider_enabled(provider):
        return redirect("/app/?video=unavailable")
    state = secrets.token_urlsafe(16)
    request.session[VIDEO_OAUTH_SESSION_KEY] = {"state": state, "provider": provider}
    return redirect(video.authorize_url(provider, _video_redirect_uri(request, provider), state))


@require_http_methods(["GET"])
def video_oauth_callback(request, provider):
    """Complete the OAuth handshake and store the connection for the tutor."""
    if not request.user.is_authenticated or request.user.role != "tutor":
        return redirect("/app/")
    pending = request.session.pop(VIDEO_OAUTH_SESSION_KEY, None) or {}
    code = request.GET.get("code", "")
    state_ok = (
        pending.get("state")
        and pending.get("state") == request.GET.get("state")
        and pending.get("provider") == provider
    )
    if not state_ok or not code or not video.provider_enabled(provider):
        return redirect("/app/?video=error")
    try:
        tokens = video.exchange_code(provider, code, _video_redirect_uri(request, provider))
    except video.VideoError:
        logger.exception("%s OAuth code exchange failed", provider)
        return redirect("/app/?video=error")
    if not tokens.get("access_token"):
        return redirect("/app/?video=error")
    # One connection per tutor: connecting (or re-connecting) replaces whatever
    # was there — a lesson only ever needs one call link.
    conn = (VideoConnection.objects.filter(tutor=request.user).first()
            or VideoConnection(tutor=request.user))
    conn.provider = provider
    video.apply_tokens(conn, tokens)
    conn.account_label = video.fetch_account_label(provider, conn.access_token)
    conn.save()
    return redirect("/app/?video=connected")


@require_http_methods(["GET", "DELETE"])
@require_roles("tutor")
def api_video_connection(request):
    """The tutor's own Zoom/Teams connection: GET shows status, DELETE unlinks.
    Tokens never leave the server — only provider + account label go out."""
    conn = VideoConnection.objects.filter(tutor=request.user).first()
    if request.method == "DELETE":
        if conn:
            conn.delete()
        return JsonResponse({"ok": True})
    return JsonResponse({
        "enabled": video.enabled_providers(),
        "connection": (
            {"provider": conn.provider, "account": conn.account_label} if conn else None
        ),
    })


# ---------------------------------------------------------------------------
# Tutor calendar subscription feed (iCal)
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_calendar_link(request):
    """The tutor's private iCal feed URL, minting the capability token on first
    use. ``{"regenerate": true}`` rotates the token, revoking every previously
    shared feed URL (mirrors the settle-link pattern)."""
    data = parse_body(request)
    tutor = acting_tutor(request, data.get("tutorSlug"))
    if tutor is None:
        return JsonResponse({"error": "tutor not found"}, status=404)
    if not tutor.calendar_token or data.get("regenerate"):
        tutor.calendar_token = secrets.token_urlsafe(24)
        tutor.save(update_fields=["calendar_token"])
    url = request.build_absolute_uri(f"/calendar/{tutor.calendar_token}.ics")
    # webcal:// is what makes Apple Calendar / Outlook subscribe (poll) instead
    # of importing a one-off snapshot.
    return JsonResponse({"url": url, "webcalUrl": re.sub(r"^https?", "webcal", url)})


@require_http_methods(["GET"])
def calendar_feed(request, token):
    """Public (token-as-capability) iCal feed of a tutor's bookings, for
    calendar apps to poll. No login: the unguessable, rotatable token on the
    tutor's account is the authorization, exactly like the settle/cancel links."""
    tutor = User.objects.filter(role="tutor", calendar_token=token).first() if token else None
    if tutor is None:
        raise Http404
    expire_booking_requests()
    # Everything upcoming plus a trailing window, so recently finished lessons
    # don't vanish from the tutor's calendar the morning after.
    since = timezone.localdate() - timedelta(days=60)
    bookings = (
        Booking.objects.filter(tutor=tutor, date__gte=since)
        .select_related("student", "tutor")
    )
    resp = HttpResponse(build_tutor_feed(tutor, bookings),
                        content_type="text/calendar; charset=utf-8")
    resp["Content-Disposition"] = 'inline; filename="thegreenpencil.ics"'
    resp["Cache-Control"] = "private, max-age=300"
    return resp
