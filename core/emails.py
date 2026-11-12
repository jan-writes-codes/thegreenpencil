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
from django.utils.html import escape
from django.utils import timezone
from django.utils.translation import get_language

from . import video
from .ical import ics_escape, ics_join
from .models import Booking, Receipt, User

logger = logging.getLogger(__name__)

_DOW = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
_MON = ["", "Jänner", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
        "September", "Oktober", "November", "Dezember"]
_DOW_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MON_EN = ["", "January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]

# Mail goes out in the recipient's language: a guest who booked on the English
# site, or an account set to English (User.language), gets templates/email/en/.
# The studio's own inbox always gets German.


def _tpl(name, lang):
    return f"email/en/{name}" if lang == "en" else f"email/{name}"


def _lang(user):
    """The language an account's mail is written in ('de' without an account)."""
    return "en" if user is not None and getattr(user, "language", "de") == "en" else "de"


def _lang_prefix(lang):
    return "/en" if lang == "en" else ""


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _date_long(d, lang="de"):
    if lang == "en":
        return f"{_DOW_EN[d.weekday()]}, {d.day} {_MON_EN[d.month]} {d.year}"
    return f"{_DOW[d.weekday()]}, {d.day}. {_MON[d.month]} {d.year}"


def _time_range(booking):
    """'14:00–14:15' for an intro, '09:00–09:45' for a 45-minute lesson."""
    end = booking.start + timedelta(minutes=booking.minutes)
    return f"{booking.time}–{end:%H:%M}"


def when(booking, lang="de"):
    """Human date/time line, e.g. 'Montag, 1. Juli 2026 · 14:00–14:15'."""
    return f"{_date_long(booking.date, lang)} · {_time_range(booking)}"


def _first(name, fallback):
    return (name or "").split(" ")[0] or fallback


def build_ics(booking, lang="de"):
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
    ]
    if lang == "en":
        lines += [
            f"SUMMARY:{ics_escape('English trial lesson · ' + tutor)}",
            f"DESCRIPTION:{ics_escape('Your free English trial lesson with ' + tutor + ' at The Green Pencil.')}",
        ]
    else:
        lines += [
            f"SUMMARY:{ics_escape('Englisch Schnupperstunde · ' + tutor)}",
            f"DESCRIPTION:{ics_escape('Deine kostenlose Englisch-Schnupperstunde mit ' + tutor + ' bei The Green Pencil.')}",
        ]
    lines += [
        f"ORGANIZER;CN={ics_escape('The Green Pencil')}:mailto:{organizer}",
    ]
    # The video-call link (auto-created via the tutor's Zoom/Teams account, or
    # hand-pasted) belongs where calendars actually show it: LOCATION + URL.
    if booking.join_link:
        lines += [f"LOCATION:{ics_escape(booking.join_link)}", f"URL:{booking.join_link}"]
    lines += [
        "STATUS:CONFIRMED",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ics_join(lines)


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


def _booking_ctx(booking, tutor_fallback, lang="de"):
    """What every booking mail shows: the tutor, the date/time and the links."""
    tutor = _tutor_name(booking, tutor_fallback)
    return {
        "tutor_name": tutor,
        # First name only — the studio addresses tutors informally everywhere else
        # in the product, so the mails read "mit Davit", not the full name.
        "tutor_first": _first(tutor, tutor),
        "when": when(booking, lang),
        "date_long": _date_long(booking.date, lang),
        "time_range": _time_range(booking),
        "site_url": settings.SITE_URL,
        # Tokenized public cancel link — works for both sides, no login required.
        # Empty if the booking predates the token.
        "cancel_url": (f"{settings.SITE_URL}{_lang_prefix(lang)}/cancel/{booking.cancel_token}/"
                       if booking.cancel_token else ""),
        # Home of the recipient's language, for the "book again" and legal links.
        "site_home": settings.SITE_URL + _lang_prefix(lang),
    }


def _ctx(booking, lang="de"):
    en = lang == "en"
    return {
        **_booking_ctx(booking, "your tutor" if en else "deiner*m Tutor*in", lang),
        "guest_name": booking.guest_name,
        "guest_first": _first(booking.guest_name, "there" if en else "du"),
        "guest_email": booking.guest_email,
        "guest_phone": booking.guest_phone,
        # The guest's public level-check result, when they took it first.
        "level_check": booking.notes if booking.is_intro else "",
        # Join link for the video call — auto-created on the tutor's connected
        # Zoom/Teams account when the booking came in, or the fixed
        # LESSON_CALL_LINK. Empty when neither exists; the templates then fall
        # back to "we'll be in touch".
        "call_link": booking.join_link,
    }


def _message(subject, to, text_body, html_body, reply_to=None):
    reply = reply_to if reply_to else (
        [settings.EMAIL_REPLY_TO] if settings.EMAIL_REPLY_TO else None
    )
    msg = EmailMultiAlternatives(
        subject=f"{settings.EMAIL_SUBJECT_PREFIX}{subject}",
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to if isinstance(to, (list, tuple)) else [to],
        reply_to=reply,
    )
    msg.attach_alternative(html_body, "text/html")
    return msg


def send_intro_notifications(booking_id, lang="de"):
    """Everything that follows a fresh intro booking, in order: first create
    the video call on the tutor's connected Zoom/Teams account, so the join
    link is already on the booking when the confirmation e-mails render; then
    mail the guest and the studio. Each step is isolated — a video-provider
    outage still sends the mails."""
    _safe(video.attach_call_link, booking_id)
    _safe(send_intro_confirmation, booking_id, lang)
    _safe(send_intro_tutor_notification, booking_id)


def intro_confirmation_message(booking, lang="de"):
    """The guest's confirmation, with the lesson as a calendar attachment."""
    ctx = _ctx(booking, lang)
    subject = (f"Your trial lesson is confirmed · {ctx['date_long']}" if lang == "en"
               else f"Deine Schnupperstunde ist bestätigt · {ctx['date_long']}")
    msg = _message(
        subject, booking.guest_email,
        render_to_string(_tpl("intro_confirmation.txt", lang), ctx),
        render_to_string(_tpl("intro_confirmation.html", lang), ctx),
    )
    filename = "trial-lesson.ics" if lang == "en" else "schnupperstunde.ics"
    msg.attach(filename, build_ics(booking, lang), "text/calendar; method=REQUEST")
    return msg


def send_intro_confirmation(booking_id, lang="de"):
    booking = Booking.objects.filter(pk=booking_id).first()
    if booking and booking.guest_email:
        intro_confirmation_message(booking, lang).send()


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


def _lesson_ctx(booking, lang="de"):
    en = lang == "en"
    name, email = _student_contact(booking)
    name = name or ("A student" if en else "Ein*e Schüler*in")
    return {
        **_booking_ctx(booking, "Tutor", lang),
        "student_name": name,
        "student_first": _first(name, "your student" if en else "dein*e Schüler*in"),
        "student_email": email,
        "title": booking.title,
    }


def send_lesson_student_confirmation(booking_id):
    """Confirm a booked paid lesson to the student, with a cancel link. No-op for
    intros (those have their own flow) or if the student has no e-mail address."""
    booking = Booking.objects.filter(pk=booking_id).first()
    if not booking or booking.is_intro:
        return
    lang = _lang(booking.student if booking.student_id else None)
    ctx = _lesson_ctx(booking, lang)
    if not ctx["student_email"]:
        return
    subject = (f"Your English lesson is booked · {ctx['date_long']} {ctx['time_range']}"
               if lang == "en" else
               f"Deine Englischstunde ist gebucht · {ctx['date_long']} {ctx['time_range']}")
    msg = _message(
        subject, ctx["student_email"],
        render_to_string(_tpl("lesson_student.txt", lang), ctx),
        render_to_string(_tpl("lesson_student.html", lang), ctx),
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
    lang = _lang(booking.tutor)
    ctx = _lesson_ctx(booking, lang)
    subject = (f"New booking: {ctx['student_name']} · {ctx['date_long']} {ctx['time_range']}"
               if lang == "en" else
               f"Neue Buchung: {ctx['student_name']} · {ctx['date_long']} {ctx['time_range']}")
    # Reply goes to the student so the tutor can answer directly.
    reply_to = [ctx["student_email"]] if ctx["student_email"] else None
    msg = _message(
        subject, to,
        render_to_string(_tpl("lesson_tutor.txt", lang), ctx),
        render_to_string(_tpl("lesson_tutor.html", lang), ctx),
        reply_to=reply_to,
    )
    msg.send()


def _notice(subject, to, ctx, reply_to=None, lang="de"):
    """Send the shared booking-notice mail (badge, heading, slot, body lines, CTA)."""
    msg = _message(
        subject, to,
        render_to_string(_tpl("booking_notice.txt", lang), ctx),
        render_to_string(_tpl("booking_notice.html", lang), ctx),
        reply_to=reply_to,
    )
    msg.send()


def send_lesson_request_student(booking_id):
    """Tell the student their booking request is in and awaits the tutor's OK."""
    booking = Booking.objects.filter(pk=booking_id).first()
    if not booking or booking.is_intro:
        return
    lang = _lang(booking.student if booking.student_id else None)
    ctx = _lesson_ctx(booking, lang)
    if not ctx["student_email"]:
        return
    if lang == "en":
        _notice(
            f"Request sent · {ctx['date_long']} {ctx['time_range']}", ctx["student_email"], {
                **ctx, "badge": "Request sent", "tone": "ok",
                "heading": f"Your request is with {ctx['tutor_first']}.",
                "who_label": "Tutor", "who": ctx["tutor_name"],
                "body": [
                    f"The slot is held for you and a credit is set aside. As soon as "
                    f"{ctx['tutor_first']} confirms, you'll get the booking confirmation.",
                    "If the request isn't confirmed, you get the credit back automatically.",
                ],
                "cta_url": f"{settings.SITE_URL}/app/", "cta_label": "Go to my bookings",
            }, lang=lang,
        )
        return
    _notice(
        f"Anfrage gesendet · {ctx['date_long']} {ctx['time_range']}", ctx["student_email"], {
            **ctx, "badge": "Anfrage gesendet", "tone": "ok",
            "heading": f"Deine Anfrage ist bei {ctx['tutor_first']}.",
            "who_label": "Tutor*in", "who": ctx["tutor_name"],
            "body": [
                f"Der Termin ist für dich reserviert und eine Einheit vorgemerkt. Sobald "
                f"{ctx['tutor_first']} bestätigt, bekommst du die Buchungsbestätigung.",
                "Wird die Anfrage nicht bestätigt, bekommst du die Einheit automatisch zurück.",
            ],
            "cta_url": f"{settings.SITE_URL}/app/", "cta_label": "Zu meinen Buchungen",
        },
    )


def send_lesson_request_tutor(booking_id):
    """Ask the lesson's tutor to confirm (or decline) a student's request."""
    booking = Booking.objects.filter(pk=booking_id).first()
    if not booking or booking.is_intro or not booking.is_requested:
        return
    to = (booking.tutor.email if booking.tutor_id and booking.tutor else "") or ""
    if not to:
        return
    lang = _lang(booking.tutor)
    ctx = _lesson_ctx(booking, lang)
    deadline = timezone.localtime(booking.request_deadline())
    if lang == "en":
        _notice(
            f"Please confirm: {ctx['student_name']} · {ctx['date_long']} {ctx['time_range']}", to, {
                **ctx, "badge": "New request", "tone": "ok",
                "heading": f"{ctx['student_name']} would like to book a lesson.",
                "who_label": "Student", "who": ctx["student_name"],
                "body": [
                    "Please confirm or decline the request in the tutor portal. Until then, "
                    "nobody else can book the slot.",
                    f"Without an answer by {deadline.strftime('%d.%m.%Y, %H:%M')}, the request is "
                    "declined automatically and the credit refunded.",
                ],
                "cta_url": f"{settings.SITE_URL}/app/", "cta_label": "View request",
            },
            reply_to=[ctx["student_email"]] if ctx["student_email"] else None, lang=lang,
        )
        return
    _notice(
        f"Bitte bestätigen: {ctx['student_name']} · {ctx['date_long']} {ctx['time_range']}", to, {
            **ctx, "badge": "Neue Anfrage", "tone": "ok",
            "heading": f"{ctx['student_name']} möchte eine Stunde buchen.",
            "who_label": "Schüler*in", "who": ctx["student_name"],
            "body": [
                "Bitte bestätige oder lehne die Anfrage im Tutor-Portal ab. Bis dahin ist "
                "der Termin für niemand anderen buchbar.",
                f"Ohne Antwort bis {deadline.strftime('%d.%m.%Y, %H:%M')} Uhr wird die Anfrage "
                "automatisch abgelehnt und die Einheit erstattet.",
            ],
            "cta_url": f"{settings.SITE_URL}/app/", "cta_label": "Anfrage ansehen",
        },
        reply_to=[ctx["student_email"]] if ctx["student_email"] else None,
    )


def send_lesson_request_declined(snapshot, reason):
    """Tell the student their request was declined / expired (credit refunded).
    Takes a ``_cancel_snapshot`` because the booking row is already gone."""
    if not snapshot.get("person_email"):
        return
    declined = reason == "declined"
    if snapshot.get("lang") == "en":
        _notice(
            f"{'Declined' if declined else 'Not confirmed'}: your request · {snapshot['date_long']}",
            snapshot["person_email"], {
                **snapshot, "badge": "Declined" if declined else "Not confirmed", "tone": "warn",
                "heading": ("Unfortunately this time doesn't work." if declined
                            else "Your request wasn't confirmed in time."),
                "who_label": "Tutor", "who": snapshot["tutor_name"],
                "body": ["The credit is back in your account. Feel free to pick another time."],
                "cta_url": f"{settings.SITE_URL}/app/", "cta_label": "Choose a new time",
            }, lang="en",
        )
        return
    _notice(
        f"{'Abgelehnt' if declined else 'Nicht bestätigt'}: deine Anfrage · {snapshot['date_long']}",
        snapshot["person_email"], {
            **snapshot, "badge": "Abgelehnt" if declined else "Nicht bestätigt", "tone": "warn",
            "heading": ("Dieser Termin passt leider nicht." if declined
                        else "Deine Anfrage wurde nicht rechtzeitig bestätigt."),
            "who_label": "Tutor*in", "who": snapshot["tutor_name"],
            "body": [
                "Die Einheit ist wieder auf deinem Konto. Such dir gern einen anderen Termin aus.",
            ],
            "cta_url": f"{settings.SITE_URL}/app/", "cta_label": "Neuen Termin wählen",
        },
    )


def _cancel_snapshot(booking, *, refunded=False):
    """Capture everything the cancellation e-mails need *before* the booking row is
    deleted, so the senders never depend on a row that no longer exists (cancelling
    deletes the booking). Mirrors the confirmation recipients: an intro notifies the
    guest and the studio inbox; a paid lesson notifies the student and that lesson's
    own tutor."""
    # A guest gets the language of the page they cancelled on (/en/cancel/...);
    # a student and a tutor get their account's language.
    if booking.is_intro:
        lang = "en" if get_language() == "en" else "de"
        tutor_lang = "de"
    else:
        lang = _lang(booking.student if booking.student_id else None)
        tutor_lang = _lang(booking.tutor if booking.tutor_id else None)
    if booking.is_intro:
        person_name, person_email = booking.guest_name or "Gast", booking.guest_email or ""
        # Intros go to the studio-wide inbox, just like the booking notification.
        tutor_email = settings.TUTOR_NOTIFY_EMAIL or ""
    else:
        person_name, person_email = _student_contact(booking)
        person_name = person_name or ("Student" if lang == "en" else "Schüler*in")
        # Paid lessons go to that lesson's own tutor, not the studio inbox.
        tutor_email = (booking.tutor.email if booking.tutor_id and booking.tutor else "") or ""
    return {
        **_booking_ctx(booking, "Tutor", lang),
        "lang": lang,
        "tutor_lang": tutor_lang,
        # The date line once more in the tutor's language, for their copy.
        "tutor_when": when(booking, tutor_lang),
        "tutor_date_long": _date_long(booking.date, tutor_lang),
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
        lang = snapshot.get("lang", "de")
        en_label = "trial lesson" if is_intro else "English lesson"
        subject = (f"Cancelled: your {en_label} · {snapshot['date_long']}" if lang == "en"
                   else f"Storniert: deine {label} · {snapshot['date_long']}")
        msg = _message(
            subject, snapshot["person_email"],
            render_to_string(_tpl("cancellation_student.txt", lang), snapshot),
            render_to_string(_tpl("cancellation_student.html", lang), snapshot),
        )
        msg.send()

    # The tutor: the lesson's own tutor for a paid lesson, the studio inbox for an intro.
    if snapshot.get("tutor_email"):
        tlang = snapshot.get("tutor_lang", "de")
        ctx = {**snapshot, "when": snapshot.get("tutor_when", snapshot["when"]),
               "date_long": snapshot.get("tutor_date_long", snapshot["date_long"])}
        subject = (f"Cancelled: {ctx['person_name']} · {'Trial lesson' if is_intro else 'English lesson'}"
                   f" · {ctx['date_long']}" if tlang == "en" else
                   f"Storniert: {ctx['person_name']} · {label} · {ctx['date_long']}")
        # Reply lands with the person who cancelled, so the tutor can follow up directly.
        reply_to = [snapshot["person_email"]] if snapshot.get("person_email") else None
        msg = _message(
            subject, snapshot["tutor_email"],
            render_to_string(_tpl("cancellation_tutor.txt", tlang), ctx),
            render_to_string(_tpl("cancellation_tutor.html", tlang), ctx),
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


_FOOTER = {"de": "The Green Pencil · Englisch-Nachhilfe", "en": "The Green Pencil · English tutoring"}


def _mail_html(lines, lang="de"):
    """A plain, branded HTML body from a list of paragraph strings."""
    body = "".join(f"<p style='margin:0 0 12px'>{ln}</p>" for ln in lines)
    return (
        "<div style=\"font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "font-size:14px;line-height:1.6;color:#243528\">"
        f"{body}"
        "<p style='margin:20px 0 0;color:#7a8b7f;font-size:12px'>"
        f"{_FOOTER[lang]}</p></div>"
    )


def _send_with_pdf(subject, to, text_lines, html_lines, pdf, pdf_name, lang="de"):
    """Send a notification with the receipt PDF attached. Bodies just describe what
    the attachment is; the PDF is the actual document."""
    text = "\n".join(text_lines) + "\n\n" + _FOOTER[lang]
    msg = _message(subject, to, text, _mail_html(html_lines, lang))
    if pdf:
        msg.attach(pdf_name, pdf, "application/pdf")
    msg.send()


def send_purchase_notifications(receipt_id):
    """Tell the student and the business that a credit purchase went through, with
    the receipt attached as a PDF.

    Covers every purchase path, but only a student's Stripe payment mails the
    student: a tutor's manual top-up notifies the business alone. If the student has
    no address (e.g. a deleted account) the business is still notified."""
    receipt = Receipt.objects.filter(pk=receipt_id).select_related("student").first()
    if not receipt:
        return
    pdf = _receipt_pdf(receipt)
    pdf_name = f"Beleg-{receipt.number}.pdf"
    student_email = (receipt.student.email if receipt.student_id and receipt.student else "") or ""
    if not receipt.stripe_session_id:
        student_email = ""  # tutor top-up: no Beleg mail to the student
    business = _business_inbox()

    if student_email and _lang(receipt.student) == "en":
        _send_with_pdf(
            f"Your receipt {receipt.number} · {receipt.credits} credits", [student_email],
            [f"Hi {receipt.student_name},", "",
             f"thank you for buying {receipt.credits} credits! Attached is your receipt "
             f"{receipt.number} from {receipt.date_str} as a PDF.",
             "Your credits are ready to use right away."],
            [f"Hi {escape(receipt.student_name)},",
             f"thank you for buying <strong>{receipt.credits} credits</strong>! Attached is "
             f"your receipt <strong>{receipt.number}</strong> from {receipt.date_str} as a PDF.",
             "Your credits are ready to use right away."],
            pdf, pdf_name, lang="en",
        )
    elif student_email:
        subject = f"Dein Beleg {receipt.number} · {receipt.credits} Einheiten"
        _send_with_pdf(
            subject, [student_email],
            [f"Hallo {receipt.student_name},", "",
             f"vielen Dank für deinen Kauf von {receipt.credits} Einheiten! Im Anhang "
             f"findest du deinen Beleg {receipt.number} vom {receipt.date_str} als PDF.",
             "Deine Einheiten stehen dir ab sofort zur Verfügung."],
            [f"Hallo {escape(receipt.student_name)},",
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
            [f"<strong>{escape(receipt.student_name)}</strong> hat "
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

    if student_email and _lang(receipt.student) == "en":
        _send_with_pdf(
            f"Cancelled: your purchase · cancellation {receipt.number}", [student_email],
            [f"Hi {receipt.student_name},", "",
             f"your purchase of {n} credits (receipt {original_no}) has been cancelled and the "
             f"amount refunded. Attached is the cancellation receipt {receipt.number} "
             f"from {receipt.date_str} as a PDF."],
            [f"Hi {escape(receipt.student_name)},",
             f"your purchase of <strong>{n} credits</strong> (receipt {original_no}) has been "
             f"cancelled and the amount refunded. Attached is the cancellation receipt "
             f"<strong>{receipt.number}</strong> from {receipt.date_str} as a PDF."],
            pdf, pdf_name, lang="en",
        )
    elif student_email:
        subject = f"Storniert: dein Kauf · Storno {receipt.number}"
        _send_with_pdf(
            subject, [student_email],
            [f"Hallo {receipt.student_name},", "",
             f"dein Kauf von {n} Einheiten (Beleg {original_no}) wurde storniert und der "
             f"Betrag erstattet. Im Anhang findest du den Storno-Beleg {receipt.number} "
             f"vom {receipt.date_str} als PDF."],
            [f"Hallo {escape(receipt.student_name)},",
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
            [f"Der Kauf von <strong>{escape(receipt.student_name)}</strong> über "
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
        "user_first": _first(user.get_full_name() or user.username,
                             "there" if _lang(user) == "en" else "du"),
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


def has_real_email(user):
    """True once the account carries a real address, not the creation placeholder."""
    return bool(_account_email(user))


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
    lang = _lang(user)
    msg = _message(
        "Reset your password · The Green Pencil" if lang == "en"
        else "Passwort zurücksetzen · The Green Pencil", to,
        render_to_string(_tpl("password_reset.txt", lang), ctx),
        render_to_string(_tpl("password_reset.html", lang), ctx),
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
    lang = _lang(user)
    msg = _message(
        "Your password was changed · The Green Pencil" if lang == "en"
        else "Dein Passwort wurde geändert · The Green Pencil", to,
        render_to_string(_tpl("password_changed.txt", lang), ctx),
        render_to_string(_tpl("password_changed.html", lang), ctx),
    )
    msg.send()


def send_email_changed(user_id, old_email):
    """Tell the *previous* address that the login e-mail was changed, with the
    same 'that wasn't me' path as the password notice: if a session was
    hijacked, the real owner still hears about it."""
    user = User.objects.filter(pk=user_id).first()
    old = (old_email or "").strip()
    if not user or not old or old.lower() == f"{user.slug}@fluent.at".lower():
        return
    lang = _lang(user)
    new = user.email or ""
    if lang == "en":
        subject = "Your login e-mail was changed · The Green Pencil"
        lines = ["Hi,", f"the login e-mail of your account was changed to {new}.",
                 f"If that wasn't you, write to {SECURITY_CONTACT} right away."]
    else:
        subject = "Deine Login-E-Mail wurde geändert · The Green Pencil"
        lines = ["Hallo,", f"die Login-E-Mail deines Kontos wurde auf {new} geändert.",
                 f"Warst das nicht du? Dann schreib bitte sofort an {SECURITY_CONTACT}."]
    msg = _message(subject, [old], "\n\n".join(lines),
                   _mail_html([escape(ln) for ln in lines], lang))
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
