"""Render one lesson (Stunde) as a printable A4 overview for the student.

The tutor downloads it from the lesson page and sends it on (e.g. via WhatsApp);
a student can download their own. It carries what the lesson page shows the
student: Themen, the summary, new vocabulary, mistakes, exercises, homework and
the list of worksheets. Never the tutor's private notes.

Same approach and look as the receipts (``receipts_pdf``): reportlab with the
built-in Helvetica, the brand mark in the header and the site's green and ink.
"""
import io
from xml.sax.saxutils import escape

from .receipts_pdf import _GREEN, _INK, _LINE, _MUTE, _draw_logo

_WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
_STORNO_RED = (0.72, 0.28, 0.17)  # muted red for the "as said" side of a mistake
_CREAM = (0.965, 0.953, 0.922)
_CREAM_LINE = (0.85, 0.83, 0.78)

# Helvetica only covers Windows-1252, so swap the few symbols tutors type often
# for plain equivalents and drop anything else it can't draw (emoji etc.).
_SUBST = {"→": "->", "⇒": "=>", "←": "<-", "✓": "ok", "✔": "ok", "✗": "x", "✘": "x", "≠": "!="}


def _clean(text):
    out = []
    for ch in str(text or ""):
        ch = _SUBST.get(ch, ch)
        try:
            ch.encode("cp1252")
        except UnicodeEncodeError:
            continue
        out.append(ch)
    return "".join(out).strip()


def _para_text(text):
    """Escape user text for a Paragraph and keep its line breaks."""
    return escape(_clean(text)).replace("\r\n", "\n").replace("\n", "<br/>")


def _end_time(hhmm, minutes):
    h, m = (int(x) for x in hhmm.split(":"))
    total = h * 60 + m + minutes
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def lesson_pdf_filename(booking):
    name = (booking.student_name or "").split(" ")[0]
    safe = "".join(ch for ch in name if ch.isalnum())
    return f"Stunde-{booking.date.isoformat()}{'-' + safe if safe else ''}.pdf"


def render_lesson_pdf(booking):
    """Return the PDF bytes for a ``Booking``'s lesson overview."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    rgb = lambda t: colors.Color(*t)  # noqa: E731
    W, H = A4
    M = 48
    HEADER_H = 72  # room for the brand mark on the first page

    when = (f"{_WEEKDAYS[booking.date.weekday()]}, {booking.date.strftime('%d.%m.%Y')} · "
            f"{booking.time}–{_end_time(booking.time, booking.minutes)}")
    student = _clean(booking.student_name)
    tutor_first = _clean(booking.tutor_name).split(" ")[0]

    # ---- Styles ------------------------------------------------------------
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14.5,
                          textColor=rgb(_INK))
    small = ParagraphStyle("small", parent=body, fontSize=8.5, leading=11.5, textColor=rgb(_MUTE))
    cell = ParagraphStyle("cell", parent=body, fontSize=9.5, leading=12.5)
    cell_b = ParagraphStyle("cellb", parent=cell, fontName="Helvetica-Bold")
    cell_mute = ParagraphStyle("cellm", parent=cell, fontSize=8.5, leading=11.5, textColor=rgb(_MUTE))
    cell_wrong = ParagraphStyle("cellw", parent=cell, textColor=rgb(_STORNO_RED))
    cell_right = ParagraphStyle("cellr", parent=cell_b, textColor=rgb(_GREEN))
    th = ParagraphStyle("th", parent=body, fontName="Helvetica-Bold", fontSize=7.5, leading=9,
                        textColor=rgb(_MUTE))
    title = ParagraphStyle("title", parent=body, fontName="Helvetica-Bold", fontSize=20, leading=24)
    meta = ParagraphStyle("meta", parent=body, textColor=rgb(_MUTE))
    h2 = ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=8.5, leading=11,
                        textColor=rgb(_GREEN))

    story = [
        Paragraph(_para_text(booking.title or "Englisch-Stunde"), title),
        Spacer(1, 4),
        Paragraph(escape(" · ".join(p for p in [student, f"mit {tutor_first}" if tutor_first else ""] if p)
                         + f" · {when}"), meta),
        Spacer(1, 8),
    ]

    def section(label, flowables, count=None):
        head = Table(
            [[Paragraph(escape(label.upper()) + (f"&nbsp;&nbsp;<font color='#8a8a85'>{count}</font>"
                                                  if count else ""), h2)]],
            colWidths=[W - 2 * M],
        )
        head.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.8, rgb(_LINE)),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        # Keep the heading with the start of its content, never orphaned at a page end.
        story.append(Spacer(1, 14))
        story.append(KeepTogether([head, Spacer(1, 7), flowables[0]]))
        story.extend(flowables[1:])

    def card_table(headers, rows, widths):
        data = [[Paragraph(escape(h), th) for h in headers]] + rows
        t = Table(data, colWidths=widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), rgb(_CREAM)),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 1), (-1, -1), 0.5, rgb(_LINE)),
        ]
        t.setStyle(TableStyle(style))
        return t

    inner = W - 2 * M
    has_content = False

    if _clean(booking.notes):
        has_content = True
        section("Themen", [Paragraph(_para_text(booking.notes), body)])

    if _clean(booking.summary):
        has_content = True
        section("Zusammenfassung", [Paragraph(_para_text(booking.summary), body)])

    cards = list(booking.error_cards.all().order_by("created_at"))
    vocab = [c for c in cards if c.kind == "vocab"]
    errors = [c for c in cards if c.kind != "vocab"]

    if vocab:
        has_content = True
        rows = [[Paragraph(_para_text(c.front), cell_b), Paragraph(_para_text(c.back), cell),
                 Paragraph(_para_text(c.note), cell_mute)] for c in vocab]
        section("Wortschatz", [card_table(["WORT", "BEDEUTUNG", "BEISPIEL"], rows,
                                          [inner * 0.28, inner * 0.30, inner * 0.42])], len(vocab))

    if errors:
        has_content = True
        rows = [[Paragraph(_para_text(c.front), cell_wrong), Paragraph(_para_text(c.back), cell_right),
                 Paragraph(_para_text(c.note), cell_mute)] for c in errors]
        section("Fehler", [card_table(["SO GESAGT", "RICHTIG", "WARUM"], rows,
                                      [inner * 0.33, inner * 0.33, inner * 0.34])], len(errors))

    exercises = list(booking.exercises.all())
    if exercises:
        has_content = True
        rows = []
        for e in exercises:
            parts = [f"<b>{_para_text(e.title)}</b>"]
            if _clean(e.description):
                parts.append(f"<font size='8.5' color='#6b6b66'>{_para_text(e.description)}</font>")
            if e.link:
                href = escape(e.link, {'"': "&quot;"})
                parts.append(f"<font size='8.5'><a href=\"{href}\" color='#309050'>"
                             f"{escape(_clean(e.link))}</a></font>")
            status = ("<font color='#309050'><b>erledigt</b></font>" if e.done
                      else "<font color='#8a8a85'>offen</font>")
            rows.append([Paragraph("<br/>".join(parts), cell),
                         Paragraph(status, ParagraphStyle("st", parent=cell, fontSize=8.5,
                                                          alignment=2))])
        t = Table(rows, colWidths=[inner - 60, 60])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, -2), 0.5, rgb(_LINE)),
        ]))
        section("Übungen", [t], len(exercises))

    if _clean(booking.homework):
        has_content = True
        box = Table([[Paragraph(_para_text(booking.homework), body)]], colWidths=[inner])
        box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), rgb(_CREAM)),
            ("BOX", (0, 0), (-1, -1), 0.8, rgb(_CREAM_LINE)),
            ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
        ]))
        section("Hausaufgabe", [box])

    sheets = list(booking.files.filter(kind="worksheet").defer("data"))
    if sheets:
        has_content = True
        items = [Paragraph(f"&bull;&nbsp;&nbsp;{_para_text(f.name)}", body) for f in sheets]
        items.append(Spacer(1, 3))
        items.append(Paragraph("Die Arbeitsblätter findest du in deinem Bereich auf thegreenpencil.at.",
                               small))
        section("Materialien", items, len(sheets))

    if not has_content:
        story.append(Spacer(1, 18))
        story.append(Paragraph("Zu dieser Stunde wurde noch nichts eingetragen.", meta))

    # ---- Page furniture ----------------------------------------------------
    def first_page(c, doc):
        top = H - M
        _draw_logo(c, M, top)
        c.setFillColorRGB(*_GREEN)
        c.setFont("Helvetica-Bold", 10)
        c.drawRightString(W - M, top - 6, "STUNDENÜBERSICHT")
        c.setFillColorRGB(*_MUTE)
        c.setFont("Helvetica", 10)
        c.drawRightString(W - M, top - 22, booking.date.strftime("%d.%m.%Y"))
        c.setStrokeColorRGB(*_LINE)
        c.setLineWidth(1)
        c.line(M, top - 60, W - M, top - 60)
        footer(c, doc)

    def footer(c, doc):
        c.setFillColorRGB(*_MUTE)
        c.setFont("Helvetica", 8.5)
        c.drawString(M, M - 18, "thegreenpencil.at")
        c.drawRightString(W - M, M - 18, f"Seite {doc.page}")

    buf = io.BytesIO()
    # The frame pads its content by 6pt; take that off the margins so text lines
    # up with the header, the rules and the full-width tables at M.
    P = 6
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=M - P, rightMargin=M - P, topMargin=M - P, bottomMargin=M - P,
        title=f"Stunde {booking.date.strftime('%d.%m.%Y')} · {student}".strip(" ·"),
        author="The Green Pencil",
    )
    # The first page reserves space under the brand header; later pages don't.
    story.insert(0, Spacer(1, HEADER_H))
    doc.build(story, onFirstPage=first_page, onLaterPages=footer)
    return buf.getvalue()
