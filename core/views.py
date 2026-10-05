import json
import logging
import secrets
from datetime import date, datetime, time as dtime, timedelta
from functools import wraps
from django.conf import settings as dj_settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError
from django.shortcuts import render, redirect
from django.http import JsonResponse, FileResponse, Http404, HttpResponse
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from .models import (
    User, Booking, CreditTransaction, Receipt, AvailabilityOverride,
    CustomTime, StudentNote, ActiveLesson, SiteSettings, LessonFile,
    VideoConnection,
)
from . import emails, video

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

# Uploaded lesson materials: an allowed type, reasonably sized.
MAX_LESSON_FILE_BYTES = 25 * 1024 * 1024  # 25 MB
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
# JS date-key helpers
#
# The frontend builds date keys with JS `Date.getMonth()`, which is 0-indexed
# (January = 0). Python's `date.month` is 1-indexed. These helpers translate
# between a real Python date and the JS "YYYY-M-D" key so DB dates stay
# semantically correct while still round-tripping with the client.
# ---------------------------------------------------------------------------
def date_to_jskey(d):
    return f"{d.year}-{d.month - 1}-{d.day}"


def jskey_to_date(key):
    year, month_idx, day = (int(p) for p in key.split("-"))
    return date(year, month_idx + 1, day)


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


def serialize_booking(b):
    return {
        "pk": b.pk,
        "studentId": booking_student_slug(b),
        "tutorId": booking_tutor_slug(b),
        "date": b.date.isoformat(),
        "time": b.time,
        "title": b.title,
        "notes": b.notes,
        "tutorNotes": b.tutor_notes,
        "callLink": b.call_link,
        # Guest "intro" bookings have no student account; the tutor UI shows the
        # guest's name/e-mail from here instead of looking them up in the roster.
        "isIntro": b.is_intro,
        "guestName": b.guest_name,
        "guestEmail": b.guest_email,
        "guestPhone": b.guest_phone,
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
        "unit": r.unit_price_cents,  # already in EUR (stored as integer EUR)
        "net": r.credits * r.unit_price_cents,
        "total": r.credits * r.unit_price_cents,
        # A Storno (credit note) receipt: negative credits/totals, and a pointer to
        # the original purchase receipt it reverses so both documents cross-reference.
        "isStorno": r.reverses_id is not None,
        "reversesNo": (r.reverses.number if r.reverses_id and r.reverses else ""),
    }


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
    import re
    m = re.search(r"\d+(?:[.,]\d+)?", str(s or ""))
    return float(m.group(0).replace(",", ".")) if m else None


def receipt_unit_price(settings, n):
    """Per-credit price for a purchase of n credits. If n matches a configured
    pack, use that pack's total ÷ n so the receipt reflects the package price
    (e.g. 10 credits -> €270, not 10 × the per-credit rate). Server-authoritative
    so a client can't dictate the price. Falls back to the per-credit rate."""
    try:
        packs = json.loads(settings.packs_json)
    except (ValueError, TypeError):
        packs = []
    for p in packs:
        try:
            if int(p.get("n")) == n:
                amt = parse_price(p.get("price"))
                if amt and n > 0:
                    return round(amt / n)
        except (ValueError, TypeError):
            continue
    return settings.credit_price


def settle_unit_euros(settings, n):
    """Per-credit price (whole euros) for settling ``n`` outstanding credits: the
    per-credit rate of the *largest configured pack whose size is ≤ n* — i.e. the
    volume tier the student has "already reached". Falls back to the flat
    per-credit price when no pack qualifies. Server-authoritative.

    Example with packs {1:€32, 5:€145, 10:€270}: settling 8 → the 5-pack tier is
    reached (€145/5 = €29) → €29/credit; settling 13 → the 10-pack tier (€27)."""
    try:
        packs = json.loads(settings.packs_json)
    except (ValueError, TypeError):
        packs = []
    best_n, best_rate = 0, None
    for p in packs:
        try:
            pn = int(p.get("n"))
            amt = parse_price(p.get("price"))
        except (ValueError, TypeError):
            continue
        if amt and pn > 0 and pn <= n and pn > best_n:
            best_n, best_rate = pn, amt / pn
    rate = best_rate if best_rate is not None else settings.credit_price
    return round(rate)


def settle_total_cents(settings, n):
    """Total settlement charge in cents for ``n`` outstanding credits (tier rule)."""
    return settle_unit_euros(settings, n) * 100 * n


def grant_credits(student, n, settings, *, label, sub, stripe_session_id="", unit_euros=None):
    """Add ``n`` credits to ``student`` and issue the matching receipt + ledger
    entry, atomically. Shared by the tutor "add credits" action and the Stripe
    checkout flow so both produce identical, auditable records.

    ``unit_euros`` overrides the per-credit price stamped on the receipt (used by
    the settlement flow, where the price follows the tier rule rather than an exact
    pack match); when omitted it falls back to ``receipt_unit_price``.

    The receipt and transaction capture a *snapshot* of the student's identity and
    billing address at issue time: these are immutable financial records that must
    stay readable verbatim even after the account is deleted (GDPR erasure).
    """
    from django.db import transaction as db_transaction
    from django.utils import timezone

    with db_transaction.atomic():
        # Lock the student row so two concurrent grants can't both read the same
        # receipt_seq and mint a duplicate receipt number.
        student = User.objects.select_for_update().get(pk=student.pk)
        student.credits += n
        student.receipt_seq += 1
        student.save()

        now = timezone.localtime()
        date_str = now.strftime("%d.%m.%Y")
        receipt_no = f"RE-{now.year}-{str(student.receipt_seq).zfill(4)}"
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
            unit_price_cents=unit_euros if unit_euros is not None else receipt_unit_price(settings, n),
            stripe_session_id=stripe_session_id,
        )

        CreditTransaction.objects.create(
            student=student,
            student_slug=student.slug,
            student_name=student_name,
            txn_type="buy",
            label=label,
            sub=sub,
            amount=n,
            receipt_no=receipt_no,
        )

    # Notify the student (with their receipt) and the business inbox that a
    # purchase went through. Deferred to on_commit so it only fires once the credit
    # grant (and any enclosing transaction) has durably committed — the sender reads
    # the persisted receipt, and an e-mail failure can never roll back or race the
    # credit write.
    from . import emails
    db_transaction.on_commit(lambda: emails.queue_email(emails.send_purchase_notifications, receipt.pk))
    return receipt


def stripe_enabled():
    """True when self-service Stripe checkout is usable (package present + key set)."""
    return bool(stripe and dj_settings.STRIPE_SECRET_KEY)


def stripe_client():
    stripe.api_key = dj_settings.STRIPE_SECRET_KEY
    return stripe


def pack_price_cents(settings, n):
    """Total price (in cents) for a pack of ``n`` credits — server-authoritative so
    a client can never dictate what it pays. Uses the configured pack price when
    ``n`` matches a pack, else falls back to the per-credit rate × n."""
    try:
        packs = json.loads(settings.packs_json)
    except (ValueError, TypeError):
        packs = []
    for p in packs:
        try:
            if int(p.get("n")) == n:
                amt = parse_price(p.get("price"))
                if amt:
                    return round(amt * 100)
        except (ValueError, TypeError):
            continue
    return settings.credit_price * 100 * n


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
    settings = get_settings()
    try:
        return grant_credits(
            student, n, settings,
            label=label, sub=sub, stripe_session_id=sid, unit_euros=unit_euros,
        )
    except IntegrityError:
        # A concurrent caller (webhook vs. redirect) won the race; reuse its receipt.
        return Receipt.objects.filter(stripe_session_id=sid).first()


def _storno_number(original_number):
    """The Storno (credit note) number paired with a purchase receipt: the same
    running number under an ``ST-`` prefix (e.g. RE-2026-1001 -> ST-2026-1001). One
    Storno per purchase — guarded by the ``cancelled`` flag — so this stays unique."""
    if original_number.startswith("RE-"):
        return "ST-" + original_number[len("RE-"):]
    return "ST-" + original_number


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
    from django.db import transaction as db_transaction
    from django.utils import timezone

    original = Receipt.objects.filter(number=txn.receipt_no).first()
    if not original:
        return None

    with db_transaction.atomic():
        # Lock the purchase row so two concurrent cancels can't both pass the guard.
        locked = CreditTransaction.objects.select_for_update().get(pk=txn.pk)
        if locked.cancelled:
            return None
        n = locked.amount  # credits originally granted (positive)

        # Snapshot identity from the purchase row; the account may since be deleted.
        student = locked.student
        student_slug = locked.student_slug
        student_name = locked.student_name
        if student is not None:
            student = User.objects.select_for_update().get(pk=student.pk)
            student.credits -= n
            student.save(update_fields=["credits"])
            student_slug = student.slug
            student_name = student.get_full_name() or student.username

        now = timezone.localtime()
        storno_no = _storno_number(original.number)
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
        refund_id = refund.get("id") if isinstance(refund, dict) else getattr(refund, "id", "")
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


def require_auth(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "auth"}, status=401)
    return None


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
    try:
        raw_packs = json.loads(settings.packs_json)
    except (ValueError, TypeError):
        raw_packs = []
    packs = []
    for p in raw_packs:
        try:
            n = int(p.get("n"))
        except (ValueError, TypeError):
            continue
        total = parse_price(p.get("price"))
        per_unit = round(total / n) if total and n > 0 else None
        # The popular badge is driven solely by SiteSettings.popular_n so it can
        # be toggled from the admin (a dropdown of pack sizes) rather than by
        # hand-editing the feat/tag fields inside packs_json.
        is_popular = n == settings.popular_n
        packs.append({
            "n": n,
            "price": p.get("price", ""),
            "per_unit": per_unit,
            "feat": is_popular,
            "tag": "Beliebteste Wahl" if is_popular else "",
        })
    # The popular pack's per-unit price is the headline figure in the pricing
    # copy — it's what nearly every student actually pays.
    featured = next((p for p in packs if p["feat"] and p["per_unit"]), None)
    return render(request, "landing.html", {"packs": packs, "featured": featured})


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


def impressum_view(request):
    # Public legal pages (Impressum / Offenlegung). Required under §5 ECG, §25 MedienG.
    return render(request, "impressum.html")


def datenschutz_view(request):
    # Public privacy notice (Datenschutzerklärung). Required under GDPR Art. 13/14.
    return render(request, "datenschutz.html")


def agb_view(request):
    # Public terms of service (Allgemeine Geschäftsbedingungen).
    return render(request, "agb.html")


def widerruf_view(request):
    # Consumer right-of-withdrawal instructions for distance contracts (FAGG).
    return render(request, "widerruf.html")


# Public, crawler-facing pages worth listing in the sitemap. The booking app and
# API endpoints are intentionally excluded — they're gated or non-content.
SITEMAP_PATHS = ["/", "/intro/", "/impressum/", "/datenschutz/", "/agb/", "/widerruf/"]


def favicon_ico(request):
    # Google Search (and older browsers) fetch /favicon.ico from the site root,
    # not the <link> in the page head. Serve the raster icon there directly so
    # the search-result icon resolves instead of falling back to a placeholder.
    from django.contrib.staticfiles import finders
    path = finders.find("favicon.ico")
    if not path:
        raise Http404("favicon.ico not found")
    return FileResponse(open(path, "rb"), content_type="image/x-icon")


def robots_txt(request):
    # Tells search engines they may crawl the site and points them at the
    # sitemap so new/updated pages get discovered faster.
    sitemap_url = request.build_absolute_uri("/sitemap.xml")
    body = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /app/",
        "Disallow: /api/",
        f"Sitemap: {sitemap_url}",
        "",
    ])
    return HttpResponse(body, content_type="text/plain")


def sitemap_xml(request):
    # Minimal XML sitemap of the public pages, with absolute URLs.
    urls = "".join(
        f"<url><loc>{request.build_absolute_uri(p)}</loc></url>"
        for p in SITEMAP_PATHS
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )
    return HttpResponse(body, content_type="application/xml")


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

    availability = {}
    for ao in AvailabilityOverride.objects.all().select_related("tutor"):
        if not ao.tutor:
            continue
        k = f"{date_to_jskey(ao.date)}|{ao.time}"
        availability.setdefault(ao.tutor.slug, {})[k] = ao.is_open

    custom_times = {}
    for ct in CustomTime.objects.all().select_related("tutor"):
        if not ct.tutor:
            continue
        dk = date_to_jskey(ct.date)
        custom_times.setdefault(ct.tutor.slug, {}).setdefault(dk, []).append(ct.time)

    # Anonymized taken slots so the calendar greys them out without leaking who
    # booked. Only today onward matters for booking.
    from django.utils import timezone
    today = timezone.localdate()
    booked = {}
    for b in Booking.objects.filter(date__gte=today).select_related("tutor"):
        slug = b.tutor.slug if b.tutor_id and b.tutor else b.tutor_slug
        if slug:
            booked.setdefault(slug, []).append(f"{date_to_jskey(b.date)}|{b.time}")

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
    return render(request, "intro.html", {"intro_data": json.dumps(data)})


import re as _re
_TIME_RE = _re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Shown to guests on every booking error so they always have a way to reach a
# human: Davit's mobile and e-mail.
_INTRO_CONTACT = "Melde dich gerne bei Davit: +43 676 397 5535 oder davit@thegreenpencil.at"


def _intro_error(message, status):
    """Guest-facing booking error: always carries Davit's contact details."""
    return JsonResponse({"error": f"{message} {_INTRO_CONTACT}"}, status=status)


@require_http_methods(["POST"])
def api_intro_booking(request):
    """Public: book a free guest intro session (no account, no credits).

    CSRF-protected like every other POST (the page carries the token). A guest is
    identified only by the name + e-mail they provide here; the booking never
    touches a User row. Capped at one intro per e-mail.
    """
    data = parse_body(request)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    tutor_slug = (data.get("tutorSlug") or "").strip()
    date_key = data.get("date") or ""
    time_str = (data.get("time") or "").strip()

    if not name:
        return _intro_error("Bitte gib deinen Namen ein.", 400)
    try:
        validate_email(email)
    except ValidationError:
        return _intro_error("Bitte gib eine gültige E-Mail-Adresse ein.", 400)
    # Phone is required so the tutor can reach the guest (WhatsApp / callback).
    if len(_re.sub(r"[^0-9]", "", phone)) < 6:
        return _intro_error(
            "Bitte gib eine gültige Telefonnummer an (für WhatsApp & Rückfragen).", 400
        )
    if not _TIME_RE.match(time_str):
        return _intro_error("Ungültige Uhrzeit.", 400)

    tutor = User.objects.filter(role="tutor", slug=tutor_slug).first()
    if tutor is None:
        return _intro_error("Tutor nicht gefunden.", 400)
    try:
        booking_date = jskey_to_date(date_key)
    except (ValueError, AttributeError, TypeError):
        return _intro_error("Ungültiger Termin.", 400)

    from django.utils import timezone
    if booking_date < timezone.localdate():
        return _intro_error("Dieser Termin liegt in der Vergangenheit.", 400)

    # One free intro per e-mail *per tutor* — a guest may try a Schnupperstunde
    # with each tutor once, but not book the same tutor twice.
    if Booking.objects.filter(
        is_intro=True, tutor=tutor, guest_email__iexact=email
    ).exists():
        return _intro_error(
            "Für diese E-Mail wurde bei diesem Tutor bereits eine Schnupperstunde gebucht.",
            409,
        )
    # Slot must be free and not explicitly closed by the tutor.
    if Booking.objects.filter(tutor=tutor, date=booking_date, time=time_str).exists():
        return _intro_error("Dieser Termin ist bereits vergeben.", 409)
    if AvailabilityOverride.objects.filter(
        tutor=tutor, date=booking_date, time=time_str, is_open=False
    ).exists():
        return _intro_error("Dieser Termin ist nicht verfügbar.", 409)

    booking = Booking.objects.create(
        tutor=tutor, student=None,
        date=booking_date, time=time_str,
        title="Schnupperstunde (Intro)",
        is_intro=True, guest_name=name, guest_email=email, guest_phone=phone,
        # Snapshot so the tutor calendar can show the guest without a User row.
        student_name=name, student_slug="intro",
        # Capability token for the cancel links in both confirmation e-mails.
        cancel_token=secrets.token_urlsafe(24),
    )
    # Fire-and-forget: create the video call (when the tutor has Zoom/Teams
    # connected) and send both confirmations — a hiccup in either must never
    # fail the booking itself. One queued job so the call link exists before
    # the e-mails render.
    from . import emails
    emails.queue_email(emails.send_intro_notifications, booking.pk)
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
    from . import emails
    booking = Booking.objects.filter(cancel_token=token).first() if token else None
    if request.method == "POST":
        if not booking:
            return render(request, "intro_cancel.html", {"state": "gone"})
        is_intro = booking.is_intro
        when = emails.booking_when(booking) if is_intro else emails.lesson_when(booking)
        # Remember the auto-created Zoom/Teams meeting (if any) before the row
        # goes away, so it can be removed from the tutor's account afterwards.
        video_ref = (booking.tutor_id, booking.video_provider, booking.video_meeting_id)
        refunded = False
        if not is_intro and booking.student_id:
            from django.db import transaction as db_transaction
            forfeit = _booking_within_24h(booking)
            with db_transaction.atomic():
                locked = User.objects.select_for_update().get(pk=booking.student_id)
                if not forfeit:
                    locked.credits += 1
                    locked.save(update_fields=["credits"])
                    _booking_credit_txn(
                        locked, "Buchung storniert — Einheit erstattet",
                        _booking_txn_sub(booking), 1,
                    )
                    refunded = True
                snapshot = emails._cancel_snapshot(booking, refunded=refunded)
                booking.delete()
        else:
            snapshot = emails._cancel_snapshot(booking, refunded=refunded)
            booking.delete()
        # Notify both sides (student/guest and tutor/studio) that it's off.
        emails.queue_email(emails.send_cancellation_notifications, snapshot)
        if video_ref[2]:
            emails.queue_email(video.cleanup_meeting, *video_ref)
        return render(request, "intro_cancel.html", {
            "state": "done", "when": when, "is_intro": is_intro,
            "refunded": refunded,
        })
    if not booking:
        return render(request, "intro_cancel.html", {"state": "gone"})
    tutor_name = booking.tutor_name or (
        booking.tutor.get_full_name() if booking.tutor_id and booking.tutor else "deinem Tutor"
    )
    return render(request, "intro_cancel.html", {
        "state": "confirm",
        "is_intro": booking.is_intro,
        "when": emails.booking_when(booking) if booking.is_intro else emails.lesson_when(booking),
        "tutor_name": (tutor_name or "").split(" ")[0] or tutor_name,
        "within_24h": (not booking.is_intro) and _booking_within_24h(booking),
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
        )
        other_bookings = list(
            Booking.objects.filter(tutor__role="tutor")
            .exclude(student=user)
            .select_related("tutor")
        )
        bookings_payload = [serialize_booking(b) for b in own_bookings] + [
            blocker_booking(b) for b in other_bookings
        ]
    else:
        bookings_payload = [
            serialize_booking(b)
            for b in Booking.objects.all().select_related("student", "tutor")
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

    # Availability overrides, keyed per tutor so two tutors' calendars never
    # collide: {tutor_slug: {date_key|time: is_open}}
    availability = {}
    for ao in AvailabilityOverride.objects.all().select_related("tutor"):
        # date_key format: "YYYY-M-D" (matches JS keyOf, 0-indexed month)
        k = f"{date_to_jskey(ao.date)}|{ao.time}"
        availability.setdefault(ao.tutor.slug, {})[k] = ao.is_open

    # Custom times, also per tutor: {tutor_slug: {date_key: [times]}}
    custom_times = {}
    for ct in CustomTime.objects.all().select_related("tutor"):
        dk = date_to_jskey(ct.date)
        custom_times.setdefault(ct.tutor.slug, {}).setdefault(dk, []).append(ct.time)

    # Student notes are tutor-private — never expose them to a student client.
    student_notes = {}
    if not is_student:
        for s in visible_students:
            notes = list(StudentNote.objects.filter(student=s).order_by("-created_at"))
            student_notes[s.slug] = [
                {"date": n.created_at.strftime("%d.%m.%Y"), "text": n.text}
                for n in notes
            ]

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
    packs = json.loads(settings.packs_json)
    for p in packs:
        try:
            is_popular = int(p.get("n")) == settings.popular_n
        except (ValueError, TypeError):
            is_popular = False
        p["feat"] = is_popular
        p["tag"] = "Beliebt" if is_popular else ""
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

    return render(request, "app.html", {"django_data": json.dumps(django_data), "role": user.role})


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
def api_change_password(request):
    """Logged-in user sets their own password — the forced step after first
    login with a handed-over temp password (or after an admin overwrote it)."""
    err = require_auth(request)
    if err:
        return err
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

def _booking_txn_sub(booking):
    """Ledger sub-line for a booking movement: which tutor, plus the lesson's
    date/time — so the credit history says who the lesson was (or would have been)
    with. Prefers the frozen snapshot, falling back to the live tutor FK."""
    tutor_name = booking.tutor_name or (
        booking.tutor.get_full_name() if booking.tutor_id and booking.tutor else ""
    )
    tutor_first = (tutor_name or "").split(" ")[0]
    when = f"{booking.date.strftime('%d.%m.%Y')} · {booking.time}"
    return f"mit {tutor_first} · {when}" if tutor_first else when


def _booking_credit_txn(student, label, sub, amount):
    """Record a booking-related movement on the credit ledger (book = -1,
    refund = +1) with the identity snapshot, mirroring grant_credits' bookkeeping.
    Returns the created transaction so callers can echo it to the client."""
    return CreditTransaction.objects.create(
        student=student,
        student_slug=student.slug,
        student_name=student.get_full_name() or student.username,
        txn_type="book" if amount < 0 else "buy",
        label=label,
        sub=sub,
        amount=amount,
    )


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
    from django.utils import timezone
    try:
        hh, mm = (int(x) for x in b.time.split(":"))
    except (ValueError, AttributeError):
        hh, mm = 0, 0
    naive = datetime.combine(b.date, dtime(hh, mm))
    start = timezone.make_aware(naive, timezone.get_current_timezone())
    return start - timezone.now() < timedelta(hours=24)


@require_http_methods(["POST"])
@require_roles("student", "tutor", "admin")
def api_bookings(request):
    from django.db import transaction as db_transaction
    data = parse_body(request)
    try:
        student = User.objects.get(slug=data["studentSlug"], role="student")
        tutor = User.objects.get(slug=data["tutorSlug"], role="tutor")
        # A student may only create bookings for themselves; tutor/admin for anyone.
        if request.user.role == "student" and student != request.user:
            return JsonResponse({"error": "forbidden"}, status=403)
        booking_date = date.fromisoformat(data["date"])
        time_str = data["time"]
        title = data.get("title", "English session")
    except (KeyError, User.DoesNotExist, ValueError) as e:
        return JsonResponse({"error": str(e)}, status=400)

    # Logging a past ("forgotten") session is a tutor/admin action, bounded to a
    # 30-day look-back so old history can't be silently rewritten. Students never
    # book in the past (their UI only offers future slots).
    from django.utils import timezone
    today = timezone.localdate()
    if booking_date < today and getattr(request.user, "role", None) in ("tutor", "admin"):
        if (today - booking_date).days > BACKDATE_LIMIT_DAYS:
            return JsonResponse(
                {"error": "too_far_back", "maxDays": BACKDATE_LIMIT_DAYS}, status=400
            )

    # The slot must be free and open — enforced here, not just in the UI, so the
    # API can't be driven into a clash or onto a closed slot.
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
            # Capability token for the cancel links in the confirmation e-mails.
            cancel_token=secrets.token_urlsafe(24),
        )
        # Booking.save() charged the credit on the locked instance; read it back.
        new_credits = locked.credits

    # Confirm to the student (with a cancel link) and notify the tutor.
    from . import emails
    emails.queue_email(emails.send_lesson_student_confirmation, b.pk)
    emails.queue_email(emails.send_lesson_tutor_notification, b.pk)
    return JsonResponse({"pk": b.pk, "credits": new_credits})


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
        from django.db import transaction as db_transaction
        from django.utils import timezone
        from . import emails
        student = b.student
        # Auto-created Zoom/Teams meeting reference, captured before the row dies.
        video_ref = (b.tutor_id, b.video_provider, b.video_meeting_id)
        # A booking whose day is already over is "abgeschlossen" in the UI, so
        # deleting it is a retroactive correction (the lesson never actually
        # happened) rather than a normal cancellation. It gets its own ledger
        # wording — naming who undid it, for a fully transparent audit trail —
        # and sends no cancellation e-mails (there is nothing to call off; the
        # refund shows up in the student's credit history instead).
        retroactive = b.date < timezone.localdate()
        refunded = False
        new_credits = None
        refund_txn = None
        if student is not None:
            # A tutor/admin removal always returns the credit; a student cancelling
            # inside the 24h window forfeits it (mirrors the booking UI's policy).
            forfeit = is_student and _booking_within_24h(b)
            with db_transaction.atomic():
                locked = User.objects.select_for_update().get(pk=student.pk)
                if not forfeit:
                    locked.credits += 1
                    locked.save(update_fields=["credits"])
                    if retroactive:
                        actor = request.user.get_full_name() or request.user.username
                        label = "Stunde rückwirkend storniert — Einheit erstattet"
                        sub = f"{_booking_txn_sub(b)} · von {actor}"
                    else:
                        label = "Buchung storniert — Einheit erstattet"
                        sub = _booking_txn_sub(b)
                    refund_txn = _booking_credit_txn(locked, label, sub, 1)
                    refunded = True
                new_credits = locked.credits
                snapshot = emails._cancel_snapshot(b, refunded=refunded)
                b.delete()
        else:
            snapshot = emails._cancel_snapshot(b, refunded=refunded)
            b.delete()
        # Notify both sides (student/guest and tutor/studio) that it's off —
        # unless this was a retroactive correction of a past lesson.
        if not retroactive:
            emails.queue_email(emails.send_cancellation_notifications, snapshot)
        if video_ref[2]:
            emails.queue_email(video.cleanup_meeting, *video_ref)
        return JsonResponse({
            "ok": True, "refunded": refunded, "credits": new_credits,
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
        b.time = data["time"]
    # A reschedule must land on a free, open slot — same guard as creating one.
    if ("date" in data or "time" in data) and b.tutor_id:
        conflict = _slot_unavailable(b.tutor, b.date, b.time, exclude_pk=b.pk)
        if conflict:
            return JsonResponse({"error": conflict}, status=409)
    if "notes" in data:
        b.notes = data["notes"]
    # tutorNotes and callLink are tutor-owned fields — students can't set them.
    if not is_student:
        if "tutorNotes" in data:
            b.tutor_notes = data["tutorNotes"]
        if "callLink" in data and data["callLink"] != b.call_link:
            # A hand-edited link supersedes the auto-created meeting: remove
            # the orphan from the tutor's account and drop the reference so a
            # later cancel/reschedule never touches the wrong call.
            if b.video_meeting_id:
                from . import emails
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
        from . import emails
        emails.queue_email(video.move_meeting, b.pk)
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Einheiten API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_credits(request, slug):
    data = parse_body(request)
    try:
        student = User.objects.get(slug=slug, role="student")
        n = int(data.get("n", 1))
        if n <= 0:
            raise ValueError("n must be positive")
    except (User.DoesNotExist, ValueError, TypeError) as e:
        return JsonResponse({"error": str(e)}, status=400)

    # Tutor top-ups are cash settled in person; card payments go through the
    # student's own Stripe checkout.
    receipt = grant_credits(
        student, n, get_settings(),
        label="Einheiten vom Tutor",
        sub="Heute · bar bezahlt",
    )
    return JsonResponse({"receipt": serialize_receipt(receipt)})


def reverse_opening_credit(txn):
    """Reverse a one-time opening balance (e.g. a fat-finger correction). Deducts the
    granted credits back off the student and records a reversing, receipt-less
    ``open`` ledger entry; marks the original cancelled so a fresh opening balance
    can be booked again. Returns the reversing transaction, or None if already
    cancelled (a concurrent caller won)."""
    from django.db import transaction as db_transaction
    with db_transaction.atomic():
        locked = CreditTransaction.objects.select_for_update().get(pk=txn.pk)
        if locked.cancelled:
            return None
        n = locked.amount
        student = locked.student
        student_slug = locked.student_slug
        student_name = locked.student_name
        if student is not None:
            student = User.objects.select_for_update().get(pk=student.pk)
            student.credits -= n
            student.save(update_fields=["credits"])
            student_slug = student.slug
            student_name = student.get_full_name() or student.username
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
    from django.db import transaction as db_transaction
    from . import emails
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

@require_http_methods(["PUT"])
def api_billing(request):
    err = require_auth(request)
    if err:
        return err
    data = parse_body(request)
    user = request.user
    if "name" in data:
        parts = (data["name"] or "").strip().split(" ", 1)
        user.first_name = parts[0]
        user.last_name = parts[1] if len(parts) > 1 else ""
        user.billing_name = data["name"]
        user.initials = compute_initials(data["name"])
    if "line1" in data:
        user.billing_line1 = data["line1"]
    if "postcode" in data:
        user.billing_postcode = data["postcode"]
    if "city" in data:
        user.billing_city = data["city"]
    if "country" in data:
        user.billing_country = data["country"] or "Österreich"
    user.save()
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

    # A purchase always issues a receipt, so the student must have a receipt
    # address on file before they can pay.
    if not billing_complete(request.user):
        return JsonResponse({"error": "billing_required"}, status=400)

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
                        "name": f"{n} Einheiten — the green pencil",
                        "description": "1 Einheit = 45 Minuten Englisch-Einzelunterricht",
                    },
                },
            }],
            # Server-trusted facts the webhook/redirect use to credit the right
            # student. The price is set above, not taken from the client.
            metadata={"student_slug": request.user.slug, "credits": str(n)},
            client_reference_id=request.user.slug,
            customer_email=request.user.email or None,
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
    if not stripe_enabled():
        return JsonResponse({"error": "stripe_disabled"}, status=503)
    payload = request.body
    sig = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    secret = dj_settings.STRIPE_WEBHOOK_SECRET
    client = stripe_client()
    try:
        if secret:
            # Raises on a bad payload or a signature that doesn't match the secret
            # (i.e. a forged event). Exception type varies across SDK versions, so
            # catch broadly here — this block only constructs the event.
            event = client.Webhook.construct_event(payload, sig, secret)
        else:
            # No secret configured (e.g. local dev): accept unverified JSON.
            event = json.loads(payload)
    except Exception:
        return JsonResponse({"error": "invalid"}, status=400)
    event_type = event.get("type") if isinstance(event, dict) else event["type"]
    if event_type == "checkout.session.completed":
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
                        "name": f"{n} offene Einheiten — the green pencil",
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
            customer_email=student.email or None,
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
    # Settling issues a receipt too — require an address first.
    if not billing_complete(request.user):
        return JsonResponse({"error": "billing_required"}, status=400)
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
    if not student.settle_token:
        student.settle_token = secrets.token_urlsafe(24)
        student.save(update_fields=["settle_token"])
    origin = request.build_absolute_uri("/").rstrip("/")
    settings = get_settings()
    return JsonResponse({
        "url": f"{origin}/settle/{student.settle_token}/",
        "outstanding": outstanding,
        "amount": settle_total_cents(settings, outstanding) // 100,
    })


def _settle_student_for_token(token):
    return (
        User.objects.filter(settle_token=token, role="student").first()
        if token else None
    )


def settle_page(request, token):
    """Public capability-URL page where a student settles outstanding credits. No
    login required — possession of the unguessable token authorizes payment."""
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
        "amount": settle_total_cents(settings, outstanding) // 100 if outstanding else 0,
        "stripe_enabled": stripe_enabled(),
        "paid": request.GET.get("paid") == "1",
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
        # date_key is "YYYY-M-D" (JS keyOf format, 0-indexed month)
        d = jskey_to_date(data["date"])
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
    except (KeyError, ValueError, IndexError) as e:
        return JsonResponse({"error": str(e)}, status=400)


# ---------------------------------------------------------------------------
# Custom Times API
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_custom_times(request):
    data = parse_body(request)
    try:
        d = jskey_to_date(data["date"])
        time_str = data["time"]
        tutor = acting_tutor(request, data.get("tutorSlug"))
        if tutor is None:
            return JsonResponse({"error": "unknown tutor"}, status=400)
        CustomTime.objects.get_or_create(tutor=tutor, date=d, time=time_str)
        return JsonResponse({"ok": True})
    except (KeyError, ValueError, IndexError) as e:
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

    from django.db import transaction as db_transaction
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
        txn = CreditTransaction.objects.create(
            student=locked,
            student_slug=locked.slug,
            student_name=locked.get_full_name() or locked.username,
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
# Lesson files API (PDF materials, shared per lesson)
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@require_roles("tutor", "admin")
def api_lesson_files(request, lesson_id):
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
        return JsonResponse({"error": "Datei zu groß (max. 25 MB)."}, status=400)
    lf = LessonFile.objects.create(
        lesson_id=lesson_id, file=f, original_name=name[:255], uploaded_by=request.user,
    )
    return JsonResponse(serialize_lesson_file(lf))


@require_http_methods(["DELETE"])
@require_roles("tutor", "admin")
def api_lesson_file_detail(request, file_id):
    try:
        lf = LessonFile.objects.get(pk=file_id)
    except LessonFile.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)
    lf.file.delete(save=False)  # remove the blob from storage too
    lf.delete()
    return JsonResponse({"ok": True})


@require_http_methods(["GET"])
def api_lesson_file_download(request, file_id):
    # Access-controlled file serving (no public MEDIA URL): students may only
    # download materials for lessons they have unlocked; tutor/admin always.
    if not request.user.is_authenticated:
        return JsonResponse({"error": "auth"}, status=401)
    try:
        lf = LessonFile.objects.get(pk=file_id)
    except LessonFile.DoesNotExist:
        raise Http404
    if request.user.role == "student" and not ActiveLesson.objects.filter(
        student=request.user, lesson_id=lf.lesson_id
    ).exists():
        return JsonResponse({"error": "forbidden"}, status=403)
    import mimetypes
    ctype = mimetypes.guess_type(lf.original_name)[0] or "application/octet-stream"
    try:
        resp = FileResponse(lf.file.open("rb"), content_type=ctype)
    except FileNotFoundError:
        raise Http404
    # Sanitize the user-supplied filename before putting it in a header. Served
    # as an attachment (+ global nosniff) so nothing renders inline.
    safe = lf.original_name.replace('"', "").replace("\r", "").replace("\n", "") or "lesson"
    resp["Content-Disposition"] = f'attachment; filename="{safe}"'
    return resp


# ---------------------------------------------------------------------------
# Receipt PDF
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
def api_receipt_pdf(request, number):
    """Serve an issued receipt (or Storno credit note) as a formatted PDF.

    Access is default-deny: the studio staff (tutor/admin) who manage billing may
    fetch any receipt; everyone else may only fetch a receipt that belongs to them.
    A logged-in student therefore can't open another student's receipt by guessing
    its number. Rendered on the fly from the frozen snapshot, so it reads the same
    forever and needs no stored file."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "auth"}, status=401)
    receipt = Receipt.objects.filter(number=number).select_related("student", "reverses").first()
    if not receipt:
        raise Http404
    is_staff = getattr(request.user, "role", None) in ("tutor", "admin")
    owns_it = receipt.student_id is not None and receipt.student_id == request.user.pk
    if not (is_staff or owns_it):
        return JsonResponse({"error": "forbidden"}, status=403)
    try:
        from .receipts_pdf import render_receipt_pdf
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
    import time as time_module
    data = parse_body(request)
    role = data.get("role", "student")
    if role not in ("student", "tutor"):
        return JsonResponse({"error": "invalid role"}, status=400)

    # Unique, collision-safe slug. The timestamp tail is near-unique, but two
    # rapid creates could clash — loop until the slug is actually free.
    prefix = "tut" if role == "tutor" else "stu"
    base = prefix + str(int(time_module.time() * 1000))[-8:]
    slug = base
    n = 2
    while User.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1

    name = (data.get("name") or "").strip()
    first_name, _, last_name = name.partition(" ")
    # Each account gets a unique, unguessable temporary password — never a shared
    # default. It is returned once (below) so the admin can hand it over; it is not
    # stored in clear text and can't be read back afterwards.
    temp_password = secrets.token_urlsafe(9)

    if role == "tutor":
        new_user = User.objects.create_user(
            username=slug, email=f"{slug}@fluent.at", password=temp_password,
            role="tutor", slug=slug,
            initials=compute_initials(name) if name else "NT",
            color1="#309050", color2="#277a42",
            must_change_password=True,  # temp password: force an own one on first login
        )
        new_user.first_name = first_name or "New"
        new_user.last_name = last_name or "Tutor"
    else:
        new_user = User.objects.create_user(
            username=slug, email=f"{slug}@fluent.at", password=temp_password,
            role="student", slug=slug,
            initials=compute_initials(name) if name else "NS",
            color1="#52a86a", color2="#2f8a4d",
            must_change_password=True,  # temp password: force an own one on first login
        )
        new_user.first_name = first_name or "New"
        new_user.last_name = last_name or "Student"
    new_user.save()
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
        name = data["name"] or ""
        parts = name.strip().split(" ", 1)
        u.first_name = parts[0]
        u.last_name = parts[1] if len(parts) > 1 else ""
        u.billing_name = name
        u.initials = compute_initials(name)
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
        # photo visible when the student later signs in on another device.
        u.photo = data["photo"] or None
    # NB: credits are intentionally NOT settable here. A free-form reset left no
    # audit trail (a fraud risk); balances now only move through booked
    # transactions — purchases, bookings, and the one-time opening balance
    # (api_opening_credit).
    if "billing" in data:
        b = data["billing"]
        if isinstance(b, dict):
            if "line1" in b:
                u.billing_line1 = b["line1"]
            if "postcode" in b:
                u.billing_postcode = b["postcode"]
            if "city" in b:
                u.billing_city = b["city"]
            if "country" in b:
                u.billing_country = b["country"] or "Österreich"
    u.save()
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
    return JsonResponse({"url": url, "webcalUrl": _re.sub(r"^https?", "webcal", url)})


@require_http_methods(["GET"])
def calendar_feed(request, token):
    """Public (token-as-capability) iCal feed of a tutor's bookings, for
    calendar apps to poll. No login: the unguessable, rotatable token on the
    tutor's account is the authorization, exactly like the settle/cancel links."""
    from django.utils import timezone
    tutor = User.objects.filter(role="tutor", calendar_token=token).first() if token else None
    if tutor is None:
        raise Http404
    # Everything upcoming plus a trailing window, so recently finished lessons
    # don't vanish from the tutor's calendar the morning after.
    since = timezone.localdate() - timedelta(days=60)
    bookings = (
        Booking.objects.filter(tutor=tutor, date__gte=since)
        .select_related("student", "tutor")
    )
    from .ical import build_tutor_feed
    resp = HttpResponse(build_tutor_feed(tutor, bookings),
                        content_type="text/calendar; charset=utf-8")
    resp["Content-Disposition"] = 'inline; filename="thegreenpencil.ics"'
    resp["Cache-Control"] = "private, max-age=300"
    return resp
