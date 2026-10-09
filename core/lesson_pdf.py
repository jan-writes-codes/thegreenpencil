"""Render one lesson (Stunde) as a printable A4 overview for the student.

The tutor downloads it from the lesson page and sends it on (e.g. via WhatsApp);
a student can download their own. It carries what the lesson page shows the
student: Themen, the summary, new vocabulary, mistakes, exercises, homework and
the list of worksheets. Never the tutor's private notes. The worksheets
themselves follow the overview, with the lesson's vocab and mistake highlights
drawn onto their pages and numbered like the rows in the tables (W1, F1, ...).

Same approach and look as the receipts (``receipts_pdf``): reportlab with the
built-in Helvetica, the brand mark in the header and the site's green and ink.
"""
import io
import logging
from xml.sax.saxutils import escape

from .receipts_pdf import _GREEN, _INK, _LINE, _MUTE, _draw_logo

logger = logging.getLogger(__name__)

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


def _split(total, *fractions):
    return [total * f for f in fractions]


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _sheet_kind(name):
    """"pdf" or "image" for a worksheet that can go into the export, else None
    (Word files, audio etc. are only listed)."""
    low = (name or "").lower()
    if low.endswith(".pdf"):
        return "pdf"
    if low.endswith(_IMAGE_EXTS):
        return "image"
    return None


# Highlight colours, as on the lesson page (.sx-hl i / i.error).
_HL = {"vocab": ((0.188, 0.565, 0.314), 0.32), "error": ((0.839, 0.502, 0.243), 0.45)}
_BADGE = {"vocab": (0.188, 0.565, 0.314), "error": (0.76, 0.44, 0.18)}


def _rect(r):
    try:
        return tuple(float(v) for v in r[:4]) if len(r) >= 4 else None
    except (TypeError, ValueError):
        return None


def _overlay_page(width, height, x0, y0, marks):
    """A one-page PDF carrying a worksheet page's highlights. ``marks`` is
    [(card, label)]; each card's rects are [x, y, w, h] fractions of the visible
    page measured from its top-left corner, as the lesson page stores them."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(x0 + width, y0 + height))
    for card, label in marks:
        kind = "vocab" if card.kind == "vocab" else "error"
        rects = [r for r in (_rect(r) for r in card.rects) if r]
        if not rects:
            continue
        rgb, alpha = _HL[kind]
        c.saveState()
        c.setFillColorRGB(*rgb)
        c.setFillAlpha(alpha)
        for fx, fy, fw, fh in rects:
            c.roundRect(x0 + fx * width, y0 + (1 - fy - fh) * height, fw * width, fh * height,
                        1.5, stroke=0, fill=1)
        c.restoreState()
        # Badge with the table label, raised like a superscript at the end of
        # the first highlight so it covers as little of the sheet as possible.
        fx, fy, fw, fh = rects[0]
        size = 5.5
        bw = c.stringWidth(label, "Helvetica-Bold", size) + 4
        bh = size + 2.5
        bx = min(x0 + (fx + fw) * width - 1, x0 + width - bw - 1)
        by = min(y0 + (1 - fy) * height - 2.5, y0 + height - bh - 1)
        c.setFillColorRGB(*_BADGE[kind])
        c.roundRect(bx, by, bw, bh, 2.5, stroke=0, fill=1)
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica-Bold", size)
        c.drawCentredString(bx + bw / 2, by + 2, label)
    c.showPage()
    c.save()
    return buf.getvalue()


def _image_page(data):
    """An A4 page with a worksheet photo or scan fitted inside the margins."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    img = ImageReader(io.BytesIO(data))
    iw, ih = img.getSize()
    W, H = A4
    M = 36
    scale = min((W - 2 * M) / iw, (H - 2 * M) / ih)
    w, h = iw * scale, ih * scale
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawImage(img, (W - w) / 2, (H - h) / 2, w, h)
    c.showPage()
    c.save()
    return buf.getvalue()


def _append_sheets(overview, appended, cards, labels):
    """The overview followed by each worksheet with its highlights drawn on.
    A sheet that can't be read (damaged, password-protected) is left out rather
    than failing the whole export."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(overview)))
    for f, kind, data in appended:
        try:
            if kind == "image":
                writer.append(PdfReader(io.BytesIO(_image_page(data))))
                continue
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                reader.decrypt("")
            pages = []
            for n, page in enumerate(reader.pages, 1):
                marks = [(c, labels[c.pk]) for c in cards
                         if c.source_file_id == f.pk and c.page == n and c.rects]
                if marks:
                    # Bake any /Rotate into the content so the page box is what
                    # was on screen, which the highlight fractions refer to.
                    if page.rotation:
                        page.transfer_rotation_to_content()
                    box = page.cropbox
                    overlay = _overlay_page(float(box.width), float(box.height),
                                            float(box.left), float(box.bottom), marks)
                    page.merge_page(PdfReader(io.BytesIO(overlay)).pages[0])
                pages.append(page)
        except Exception:
            logger.warning("lesson pdf: worksheet %s left out", f.pk, exc_info=True)
            continue
        for page in pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def render_lesson_pdf(booking, sheet_bytes=None):
    """Return the PDF bytes for a ``Booking``'s lesson overview with its
    worksheets appended. ``sheet_bytes(session_file)`` returns a worksheet's
    content (or None when it's gone); by default the file's own stored bytes."""
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
    # Row labels shared by the tables and the badges on the worksheet pages.
    labels = {c.pk: f"W{i}" for i, c in enumerate(vocab, 1)}
    labels.update({c.pk: f"F{i}" for i, c in enumerate(errors, 1)})

    sheets = list(booking.files.filter(kind="worksheet").defer("data"))
    appended = []  # (session file, kind, bytes) for the pages after the overview
    for f in sheets:
        kind = _sheet_kind(f.name)
        if not kind:
            continue
        try:
            data = sheet_bytes(f) if sheet_bytes else (bytes(f.data) if f.data is not None else None)
        except Exception:
            data = None
        if data:
            appended.append((f, kind, data))
    marked = {c.pk for c in cards if c.source_file_id and c.page and c.rects
              and any(f.pk == c.source_file_id and k == "pdf" for f, k, _ in appended)}

    def label_cell(c, color):
        if c.pk not in marked:
            return ""
        return Paragraph(f"<font color='{color}'><b>{labels[c.pk]}</b></font>", cell_mute)

    if vocab:
        has_content = True
        rows = [[label_cell(c, "#309050"), Paragraph(_para_text(c.front), cell_b),
                 Paragraph(_para_text(c.back), cell), Paragraph(_para_text(c.note), cell_mute)]
                for c in vocab]
        section("Wortschatz", [card_table(["", "WORT", "BEDEUTUNG", "BEISPIEL"], rows,
                                          [30] + _split(inner - 30, 0.28, 0.30, 0.42))], len(vocab))

    if errors:
        has_content = True
        rows = [[label_cell(c, "#c2702f"), Paragraph(_para_text(c.front), cell_wrong),
                 Paragraph(_para_text(c.back), cell_right), Paragraph(_para_text(c.note), cell_mute)]
                for c in errors]
        section("Fehler", [card_table(["", "SO GESAGT", "RICHTIG", "WARUM"], rows,
                                      [30] + _split(inner - 30, 0.33, 0.33, 0.34))], len(errors))

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

    if sheets:
        has_content = True
        included = {f.pk for f, _, _ in appended}
        items = [Paragraph(f"&bull;&nbsp;&nbsp;{_para_text(f.name)}"
                           + ("" if f.pk in included
                              else "&nbsp;&nbsp;<font size='8.5' color='#8a8a85'>(nicht in diesem PDF)</font>"),
                           body) for f in sheets]
        items.append(Spacer(1, 3))
        items.append(Paragraph(
            ("Die Arbeitsblätter folgen auf den nächsten Seiten, mit den markierten Wörtern und Fehlern. "
             if included else "")
            + "Alle Dateien findest du in deinem Bereich auf thegreenpencil.at.", small))
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
    if not appended:
        return buf.getvalue()
    return _append_sheets(buf.getvalue(), appended, cards, labels)
