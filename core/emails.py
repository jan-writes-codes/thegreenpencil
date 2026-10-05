"""Transactional e-mail for the booking flow.

Sent through Django's mail framework, so the backend is swappable: Resend (via
django-anymail) in production, console in dev, locmem in tests. Nothing here
requires e-mail to be configured — if it isn't, mail is printed to the console
and the booking still succeeds.

Each public entry point takes a booking *id* (not the object) so it is safe to
run on a background thread or, later, a durable queue (see ``queue_email``).
"""
import logging
import threading
from datetime import timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from . import video
from .ical import ics_escape
from .models import Booking, Receipt, User

logger = logging.getLogger(__name__)

_DOW = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
_MON = ["", "Jänner", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
        "September", "Oktober", "November", "Dezember"]


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _date_long(d):
    return f"{_DOW[d.weekday()]}, {d.day}. {_MON[d.month]} {d.year}"


def _time_range(booking):
    """'14:00–14:15' for an intro, '09:00–09:45' for a 45-minute lesson."""
    end = booking.start + timedelta(minutes=booking.minutes)
    return f"{booking.time}–{end:%H:%M}"


def when(booking):
    """Human German date/time line, e.g. 'Montag, 1. Juli 2026 · 14:00–14:15'."""
    return f"{_date_long(booking.date)} · {_time_range(booking)}"


def _first(name, fallback):
    return (name or "").split(" ")[0] or fallback


def build_ics(booking):
    """A minimal, valid VCALENDAR for the lesson so the guest can add it to their
    calendar in one tap. Times are emitted in UTC to avoid shipping a VTIMEZONE."""
    start = booking.start.astimezone(dt_timezone.utc)
    end = start + timedelta(minutes=booking.minutes)
    stamp = timezone.now().astimezone(dt_timezone.utc)
    fmt = "%Y%m%dT%H%M%SZ"
    tutor = booking.tutor_name or (booking.tutor.get_full_name() if booking.tutor_id and booking.tutor else "The Green Pencil")
    organizer = settings.EMAIL_REPLY_TO or "hallo@thegreenpencil.at"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//The Green Pencil//Booking//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:intro-{booking.pk}@thegreenpencil.at",
        f"DTSTAMP:{stamp.strftime(fmt)}",
        f"DTSTART:{start.strftime(fmt)}",
        f"DTEND:{end.strftime(fmt)}",
        f"SUMMARY:{ics_escape('Englisch Schnupperstunde · ' + tutor)}",
        f"DESCRIPTION:{ics_escape('Deine kostenlose Englisch-Schnupperstunde mit ' + tutor + ' bei The Green Pencil.')}",
        f"ORGANIZER;CN={ics_escape('The Green Pencil')}:mailto:{organizer}",
    ]
    # The video-call link (auto-created via the tutor's Zoom/Teams account, or
    # hand-pasted) belongs where calendars actually show it: LOCATION + URL.
    if booking.call_link:
        lines += [f"LOCATION:{ics_escape(booking.call_link)}", f"URL:{ics_escape(booking.call_link)}"]
    lines += [
        "STATUS:CONFIRMED",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    # iCalendar lines are CRLF-terminated.
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------------------------- #
# Senders (take an id so they're safe to run off-thread)
# --------------------------------------------------------------------------- #
def _tutor_name(booking, fallback):
    return booking.tutor_name or (
        booking.tutor.get_full_name() if booking.tutor_id and booking.tutor else ""
    ) or fallback


def _student_contact(booking):
    """(name, email) of a lesson's student: the live account, else the snapshot."""
    student = booking.student if booking.student_id else None
    if student:
        return student.get_full_name() or student.username, student.email or ""
    return booking.student_name, ""


def _booking_ctx(booking, tutor_fallback):
    """What every booking mail shows: the tutor, the date/time and the links."""
    tutor = _tutor_name(booking, tutor_fallback)
    return {
        "tutor_name": tutor,
        # First name only — the studio addresses tutors informally everywhere else
        # in the product, so the mails read "mit Davit", not the full name.
        "tutor_first": _first(tutor, tutor),
        "when": when(booking),
        "date_long": _date_long(booking.date),
        "time_range": _time_range(booking),
        "site_url": settings.SITE_URL,
        # Tokenized public cancel link — works for both sides, no login required.
        # Empty if the booking predates the token.
        "cancel_url": (f"{settings.SITE_URL}/cancel/{booking.cancel_token}/"
                       if booking.cancel_token else ""),
    }


def _ctx(booking):
    return {
        **_booking_ctx(booking, "deinem Tutor"),
        "guest_name": booking.guest_name,
        "guest_first": _first(booking.guest_name, "du"),
        "guest_email": booking.guest_email,
        "guest_phone": booking.guest_phone,
        # Join link for the video call — auto-created on the tutor's connected
        # Zoom/Teams account when the booking came in. Empty when the tutor has
        # no connection; the templates then fall back to "we'll be in touch".
        "call_link": booking.call_link,
    }


def _message(subject, to, text_body, html_body, reply_to=None):
    reply = reply_to if reply_to else (
        [settings.EMAIL_REPLY_TO] if settings.EMAIL_REPLY_TO else None
    )
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to if isinstance(to, (list, tuple)) else [to],
        reply_to=reply,
    )
    msg.attach_alternative(html_body, "text/html")
    return msg


def send_intro_notifications(booking_id):
    """Everything that follows a fresh intro booking, in order: first create
    the video call on the tutor's connected Zoom/Teams account, so the join
    link is already on the booking when the confirmation e-mails render; then
    mail the guest and the studio. Each step is isolated — a video-provider
    outage still sends the mails."""
    _safe(video.attach_call_link, booking_id)
    _safe(send_intro_confirmation, booking_id)
    _safe(send_intro_tutor_notification, booking_id)


def intro_confirmation_message(booking):
    """The guest's confirmation, with the lesson as a calendar attachment."""
    ctx = _ctx(booking)
    msg = _message(
        f"Deine Schnupperstunde ist bestätigt · {ctx['date_long']}", booking.guest_email,
        render_to_string("email/intro_confirmation.txt", ctx),
        render_to_string("email/intro_confirmation.html", ctx),
    )
    msg.attach("schnupperstunde.ics", build_ics(booking), "text/calendar; method=REQUEST")
    return msg


def send_intro_confirmation(booking_id):
    booking = Booking.objects.filter(pk=booking_id).first()
    if booking and booking.guest_email:
        intro_confirmation_message(booking).send()


def send_intro_tutor_notification(booking_id):
    """Alert the studio inbox that a new intro was booked. No-op if unconfigured."""
    to = settings.TUTOR_NOTIFY_EMAIL
    if not to:
        return
    booking = Booking.objects.filter(pk=booking_id).first()
    if not booking:
        return
    ctx = _ctx(booking)
    subject = f"Neue Schnupperstunde: {ctx['guest_name']} · {ctx['date_long']}"
    msg = _message(
        subject, to,
        render_to_string("email/intro_tutor.txt", ctx),
        render_to_string("email/intro_tutor.html", ctx),
    )
    msg.send()


def _lesson_ctx(booking):
    name, email = _student_contact(booking)
    name = name or "Ein Schüler"
    return {
        **_booking_ctx(booking, "Tutor"),
        "student_name": name,
        "student_first": _first(name, "dein Schüler"),
        "student_email": email,
        "title": booking.title,
    }


def send_lesson_student_confirmation(booking_id):
    """Confirm a booked paid lesson to the student, with a cancel link. No-op for
    intros (those have their own flow) or if the student has no e-mail address."""
    booking = Booking.objects.filter(pk=booking_id).first()
    if not booking or booking.is_intro:
        return
    ctx = _lesson_ctx(booking)
    if not ctx["student_email"]:
        return
    subject = f"Deine Englischstunde ist gebucht · {ctx['date_long']} {ctx['time_range']}"
    msg = _message(
        subject, ctx["student_email"],
        render_to_string("email/lesson_student.txt", ctx),
        render_to_string("email/lesson_student.html", ctx),
    )
    msg.send()


def send_lesson_tutor_notification(booking_id):
    """Notify the tutor that a student booked (and spent a credit on) a lesson.

    Goes to the tutor's own e-mail address — not the studio-wide
    ``TUTOR_NOTIFY_EMAIL`` used for intros — so the right tutor hears about
    their own bookings. No-op for intros or if the tutor has no address."""
    booking = Booking.objects.filter(pk=booking_id).first()
    if not booking or booking.is_intro:
        return
    to = (booking.tutor.email if booking.tutor_id and booking.tutor else "") or ""
    if not to:
        return
    ctx = _lesson_ctx(booking)
    subject = f"Neue Buchung: {ctx['student_name']} · {ctx['date_long']} {ctx['time_range']}"
    # Reply goes to the student so the tutor can answer directly.
    reply_to = [ctx["student_email"]] if ctx["student_email"] else None
    msg = _message(
        subject, to,
        render_to_string("email/lesson_tutor.txt", ctx),
        render_to_string("email/lesson_tutor.html", ctx),
        reply_to=reply_to,
    )
    msg.send()


def _cancel_snapshot(booking, *, refunded=False):
    """Capture everything the cancellation e-mails need *before* the booking row is
    deleted, so the senders never depend on a row that no longer exists (cancelling
    deletes the booking). Mirrors the confirmation recipients: an intro notifies the
    guest and the studio inbox; a paid lesson notifies the student and that lesson's
    own tutor."""
    if booking.is_intro:
        person_name, person_email = booking.guest_name or "Gast", booking.guest_email or ""
        # Intros go to the studio-wide inbox, just like the booking notification.
        tutor_email = settings.TUTOR_NOTIFY_EMAIL or ""
    else:
        person_name, person_email = _student_contact(booking)
        person_name = person_name or "Schüler"
        # Paid lessons go to that lesson's own tutor, not the studio inbox.
        tutor_email = (booking.tutor.email if booking.tutor_id and booking.tutor else "") or ""
    return {
        **_booking_ctx(booking, "Tutor"),
        "is_intro": booking.is_intro,
        "person_name": person_name,
        "person_first": _first(person_name, "du"),
        "person_email": person_email,
        "tutor_email": tutor_email,
        # Only meaningful for paid lessons — whether the credit was returned.
        "refunded": refunded,
    }


def send_cancellation_notifications(snapshot):
    """E-mail both sides that a booking was cancelled. Takes a snapshot dict (see
    ``_cancel_snapshot``) rather than a booking id, because the row is already gone by
    the time this runs. Skips any recipient without an address."""
    is_intro = snapshot["is_intro"]
    label = "Schnupperstunde" if is_intro else "Englischstunde"

    # The person who booked: the guest for an intro, the student for a paid lesson.
    if snapshot.get("person_email"):
        subject = f"Storniert: deine {label} · {snapshot['date_long']}"
        msg = _message(
            subject, snapshot["person_email"],
            render_to_string("email/cancellation_student.txt", snapshot),
            render_to_string("email/cancellation_student.html", snapshot),
        )
        msg.send()

    # The tutor: the lesson's own tutor for a paid lesson, the studio inbox for an intro.
    if snapshot.get("tutor_email"):
        subject = f"Storniert: {snapshot['person_name']} · {label} · {snapshot['date_long']}"
        # Reply lands with the person who cancelled, so the tutor can follow up directly.
        reply_to = [snapshot["person_email"]] if snapshot.get("person_email") else None
        msg = _message(
            subject, snapshot["tutor_email"],
            render_to_string("email/cancellation_tutor.txt", snapshot),
            render_to_string("email/cancellation_tutor.html", snapshot),
            reply_to=reply_to,
        )
        msg.send()


def _business_inbox():
    """The studio/business address that hears about purchases and cancellations —
    the dedicated notify inbox if configured, else the studio's own From address."""
    return settings.TUTOR_NOTIFY_EMAIL or settings.DEFAULT_FROM_EMAIL


def _receipt_pdf(receipt):
    """Render a receipt (purchase or Storno) to PDF bytes, or None if it can't be
    produced. Lazy import: views imports this module, so importing at module load
    would be circular; reportlab is imported inside the renderer."""
    try:
        from .receipts_pdf import render_receipt_pdf
        return render_receipt_pdf(receipt)
    except Exception:
        logger.exception("receipt PDF render failed for %s", getattr(receipt, "number", "?"))
        return None


def _mail_html(lines):
    """A plain, branded HTML body from a list of paragraph strings."""
    body = "".join(f"<p style='margin:0 0 12px'>{ln}</p>" for ln in lines)
    return (
        "<div style=\"font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "font-size:14px;line-height:1.6;color:#243528\">"
        f"{body}"
        "<p style='margin:20px 0 0;color:#7a8b7f;font-size:12px'>"
        "The Green Pencil — Englisch-Nachhilfe</p></div>"
    )


def _send_with_pdf(subject, to, text_lines, html_lines, pdf, pdf_name):
    """Send a notification with the receipt PDF attached. Bodies just describe what
    the attachment is; the PDF is the actual document."""
    text = "\n".join(text_lines) + "\n\nThe Green Pencil — Englisch-Nachhilfe"
    msg = _message(subject, to, text, _mail_html(html_lines))
    if pdf:
        msg.attach(pdf_name, pdf, "application/pdf")
    msg.send()


def send_purchase_notifications(receipt_id):
    """Tell the student and the business that a credit purchase went through, with
    the receipt attached as a PDF.

    Covers every purchase path — a tutor's cash top-up or a student's Stripe
    payment. If the student has no address (e.g. a deleted account) the business is
    still notified."""
    receipt = Receipt.objects.filter(pk=receipt_id).select_related("student").first()
    if not receipt:
        return
    pdf = _receipt_pdf(receipt)
    pdf_name = f"Beleg-{receipt.number}.pdf"
    student_email = (receipt.student.email if receipt.student_id and receipt.student else "") or ""
    business = _business_inbox()

    if student_email:
        subject = f"Dein Beleg {receipt.number} · {receipt.credits} Einheiten"
        _send_with_pdf(
            subject, [student_email],
            [f"Hallo {receipt.student_name},", "",
             f"vielen Dank für deinen Kauf von {receipt.credits} Einheiten! Im Anhang "
             f"findest du deinen Beleg {receipt.number} vom {receipt.date_str} als PDF.",
             "Deine Einheiten stehen dir ab sofort zur Verfügung."],
            [f"Hallo {receipt.student_name},",
             f"vielen Dank für deinen Kauf von <strong>{receipt.credits} Einheiten</strong>! "
             f"Im Anhang findest du deinen Beleg <strong>{receipt.number}</strong> vom "
             f"{receipt.date_str} als PDF.",
             "Deine Einheiten stehen dir ab sofort zur Verfügung."],
            pdf, pdf_name,
        )

    # Notify the business — skip only if it's the very same address the student got.
    if business and business != student_email:
        subject = f"Neuer Kauf: {receipt.student_name} · {receipt.credits} Einheiten ({receipt.number})"
        _send_with_pdf(
            subject, [business],
            [f"{receipt.student_name} hat {receipt.credits} Einheiten gekauft.",
             f"Der Beleg {receipt.number} vom {receipt.date_str} ist als PDF angehängt."],
            [f"<strong>{receipt.student_name}</strong> hat "
             f"<strong>{receipt.credits} Einheiten</strong> gekauft.",
             f"Der Beleg <strong>{receipt.number}</strong> vom {receipt.date_str} ist als PDF angehängt."],
            pdf, pdf_name,
        )


def send_storno_notifications(receipt_id):
    """Tell the student and the business that a purchase was cancelled, with the
    Storno credit note attached as a PDF.

    Takes the *Storno* receipt id. Skips any recipient without an address (e.g. a
    deleted student account)."""
    receipt = Receipt.objects.filter(pk=receipt_id).select_related("student", "reverses").first()
    if not receipt:
        return
    pdf = _receipt_pdf(receipt)
    pdf_name = f"Storno-{receipt.number}.pdf"
    student_email = (receipt.student.email if receipt.student_id and receipt.student else "") or ""
    business = _business_inbox()
    n = -receipt.credits  # credits reversed (receipt.credits is negative)
    original_no = receipt.reverses.number if receipt.reverses_id and receipt.reverses else ""

    if student_email:
        subject = f"Storniert: dein Kauf · Storno {receipt.number}"
        _send_with_pdf(
            subject, [student_email],
            [f"Hallo {receipt.student_name},", "",
             f"dein Kauf von {n} Einheiten (Beleg {original_no}) wurde storniert und der "
             f"Betrag erstattet. Im Anhang findest du den Storno-Beleg {receipt.number} "
             f"vom {receipt.date_str} als PDF."],
            [f"Hallo {receipt.student_name},",
             f"dein Kauf von <strong>{n} Einheiten</strong> (Beleg {original_no}) wurde "
             f"storniert und der Betrag erstattet. Im Anhang findest du den Storno-Beleg "
             f"<strong>{receipt.number}</strong> vom {receipt.date_str} als PDF."],
            pdf, pdf_name,
        )

    if business and business != student_email:
        subject = f"Storniert: {receipt.student_name} · {n} Einheiten (Storno {receipt.number})"
        _send_with_pdf(
            subject, [business],
            [f"Der Kauf von {receipt.student_name} über {n} Einheiten (Beleg {original_no}) "
             f"wurde storniert und erstattet.",
             f"Der Storno-Beleg {receipt.number} vom {receipt.date_str} ist als PDF angehängt."],
            [f"Der Kauf von <strong>{receipt.student_name}</strong> über "
             f"<strong>{n} Einheiten</strong> (Beleg {original_no}) wurde storniert und erstattet.",
             f"Der Storno-Beleg <strong>{receipt.number}</strong> vom {receipt.date_str} ist als PDF angehängt."],
            pdf, pdf_name,
        )


# --------------------------------------------------------------------------- #
# Password reset / change
# --------------------------------------------------------------------------- #
# Where a user turns when a "your password was changed" notice wasn't them.
SECURITY_CONTACT = "davit@thegreenpencil.at"


def _account_ctx(user):
    return {
        "user_first": _first(user.get_full_name() or user.username, "du"),
        "user_email": user.email,
        "site_url": settings.SITE_URL,
        "security_contact": SECURITY_CONTACT,
    }


def _account_email(user):
    """The address account mail may go to — or '' when none is on file yet.
    Fresh accounts carry the auto-generated `<slug>@fluent.at` placeholder until
    the admin enters the real address; mailing that would only bounce."""
    email = (user.email or "").strip()
    if not email or email.lower() == f"{user.slug}@fluent.at".lower():
        return ""
    return email


def send_password_reset(user_id, reset_url):
    """The 'Passwort vergessen' mail with the tokenized reset link."""
    user = User.objects.filter(pk=user_id).first()
    if not user:
        return
    to = _account_email(user)
    if not to:
        return
    ctx = _account_ctx(user)
    ctx["reset_url"] = reset_url
    msg = _message(
        "Passwort zurücksetzen · The Green Pencil", to,
        render_to_string("email/password_reset.txt", ctx),
        render_to_string("email/password_reset.html", ctx),
    )
    msg.send()


def send_password_changed(user_id):
    """Confirmation that the password was changed — with a 'that wasn't me'
    escalation path, so a hijacked account gets noticed."""
    user = User.objects.filter(pk=user_id).first()
    if not user:
        return
    to = _account_email(user)
    if not to:
        return
    ctx = _account_ctx(user)
    msg = _message(
        "Dein Passwort wurde geändert · The Green Pencil", to,
        render_to_string("email/password_changed.txt", ctx),
        render_to_string("email/password_changed.html", ctx),
    )
    msg.send()


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def _safe(func, *args):
    try:
        func(*args)
    except Exception:  # never let an e-mail failure break the booking
        logger.exception("transactional e-mail failed: %s", getattr(func, "__name__", func))


def queue_email(func, *args):
    """Run a sender without blocking the request. A daemon thread is enough for a
    single-studio app; swap this one function for a durable queue (Django Q2 /
    Celery) when volume warrants — the call sites don't change."""
    if getattr(settings, "EMAIL_ASYNC", False):
        threading.Thread(target=_safe, args=(func, *args), daemon=True).start()
    else:
        _safe(func, *args)
