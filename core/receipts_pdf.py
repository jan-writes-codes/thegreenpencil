"""Render an issued receipt (or Storno credit note) as a nicely formatted A4 PDF.

Kept separate from views so it can be reused by the download endpoint and the
transactional e-mails. reportlab is a pure-Python dependency (no system libs),
so this imports and runs anywhere the app does. The layout mirrors the on-screen
receipt (``receiptDocHtml`` in app.html): brand mark, receipt meta, supplier/recipient
blocks, a single line item, totals, the VAT-exemption note and a footer.
"""
import io

# --------------------------------------------------------------------------- #
# Static content — must match the on-screen receipt and the published Impressum
# --------------------------------------------------------------------------- #
SUPPLIER = {
    "name": "Davit Hovakimyan",
    "line2": "Englisch-Privatunterricht",
    "addr": "Kraygasse 94/2/6",
    "city": "1220 Wien, Österreich",
    "email": "davit@thegreenpencil.at",
}
VAT_DE = "Steuerfrei gemäß § 6 Abs. 1 Z 11 UStG (Unterrichtsleistung eines Privatlehrers)."
VAT_EN = "VAT-exempt educational service under Austrian law — no value-added tax is charged."

# Brand palette (matches the site's --c1 grass green and ink tones).
_GREEN = (0.188, 0.565, 0.314)
_INK = (0.141, 0.208, 0.157)
_MUTE = (0.42, 0.42, 0.40)
_LINE = (0.83, 0.82, 0.78)
_STORNO = (0.72, 0.28, 0.17)


def _money(x):
    """German-formatted euro amount, matching the on-screen receipt (comma, 2dp)."""
    return f"{x:.2f}".replace(".", ",")


def _draw_logo(c, x, top):
    """Draw the brand mark + wordmark exactly like the site header: a green
    rounded tile with a white pencil (see static/favicon.svg), then "the green"
    in ink and "pencil" in green, with the letterspaced subtitle. Returns nothing;
    the mark occupies a ~46pt square at (x, top-46)."""
    from reportlab.lib import colors

    S = 46.0            # tile size
    tile_x, tile_y = x, top - S
    scale = S / 32.0    # favicon.svg uses a 32×32 viewBox
    radius = 9 * scale

    # Green rounded tile — subtle gradient like the site, solid green as fallback.
    c.saveState()
    try:
        path = c.beginPath()
        path.roundRect(tile_x, tile_y, S, S, radius)
        c.clipPath(path, stroke=0, fill=0)
        c.linearGradient(
            tile_x, tile_y + S, tile_x + S, tile_y,
            (colors.HexColor("#4cb56b"), colors.HexColor("#309050")),
            (0.0, 0.72),
        )
    except Exception:
        c.setFillColorRGB(*_GREEN)
        c.roundRect(tile_x, tile_y, S, S, radius, stroke=0, fill=1)
    c.restoreState()

    # White pencil — same strokes as favicon.svg, y-flipped into the tile.
    def P(sx, sy):
        return (tile_x + sx * scale, tile_y + (32 - sy) * scale)

    c.saveState()
    c.setStrokeColorRGB(1, 1, 1)
    c.setLineWidth(2.2 * scale)
    c.setLineCap(1)
    c.setLineJoin(1)
    body = c.beginPath()
    bx, by = P(20, 8.5)
    body.moveTo(bx, by)
    for sx, sy in [(23.5, 12), (13.7, 21.8), (9.5, 22.5), (10.2, 18.3)]:
        bx, by = P(sx, sy)
        body.lineTo(bx, by)
    body.close()
    c.drawPath(body, stroke=1, fill=0)
    band = c.beginPath()
    bx, by = P(17.7, 10.8)
    band.moveTo(bx, by)
    bx, by = P(21.2, 14.3)
    band.lineTo(bx, by)
    c.drawPath(band, stroke=1, fill=0)
    c.restoreState()

    # Wordmark: "the green " (ink) + "pencil" (green), then the subtitle.
    tx = tile_x + S + 14
    c.setFont("Helvetica-Bold", 17)
    c.setFillColorRGB(*_INK)
    c.drawString(tx, top - 20, "the green ")
    w1 = c.stringWidth("the green ", "Helvetica-Bold", 17)
    c.setFillColorRGB(*_GREEN)
    c.drawString(tx + w1, top - 20, "pencil")
    c.setFillColorRGB(*_MUTE)
    c.setFont("Helvetica", 8)
    # Letterspaced subtitle, drawn char-by-char (portable across reportlab builds).
    sx = tx + 1
    for ch in "ENGLISCH-NACHHILFE":
        c.drawString(sx, top - 34, ch)
        sx += c.stringWidth(ch, "Helvetica", 8) + 1.7


def render_receipt_pdf(receipt):
    """Return the PDF bytes for a ``Receipt`` instance (purchase or Storno)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    is_storno = receipt.reverses_id is not None
    reverses_no = receipt.reverses.number if is_storno and receipt.reverses else ""
    credits = receipt.credits
    unit = receipt.unit_price_cents
    net = credits * unit

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    # PDF /Title — what the browser tab / viewer shows (e.g. "Beleg RE-2026-1001").
    c.setTitle(f"{'Storno' if is_storno else 'Beleg'} {receipt.number}")
    c.setAuthor("The Green Pencil")
    W, H = A4
    M = 48  # page margin
    right = W - M

    # ---- Header: brand mark + name, and receipt meta on the right ----------
    top = H - M
    _draw_logo(c, M, top)

    head_label = "STORNO · CANCELLATION" if is_storno else "BELEG · RECEIPT"
    c.setFillColorRGB(*(_STORNO if is_storno else _GREEN))
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(right, top - 6, head_label)
    c.setFillColorRGB(*_INK)
    c.setFont("Helvetica", 10)
    c.drawRightString(right, top - 22, f"Nr. {receipt.number}")
    c.setFillColorRGB(*_MUTE)
    c.drawRightString(right, top - 36, receipt.date_str)

    y = top - 60
    c.setStrokeColorRGB(*_LINE)
    c.setLineWidth(1)
    c.line(M, y, right, y)
    y -= 24

    # ---- Storno note -------------------------------------------------------
    if is_storno:
        c.setFillColorRGB(0.97, 0.93, 0.90)
        c.setStrokeColorRGB(*_STORNO)
        c.roundRect(M, y - 34, right - M, 40, 7, stroke=1, fill=1)
        c.setFillColorRGB(*_STORNO)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(M + 12, y - 8, f"Storno-Beleg (Gutschrift) zu Beleg Nr. {reverses_no}.")
        c.setFont("Helvetica", 9.5)
        c.drawString(M + 12, y - 22,
                     "Der ursprüngliche Kauf wurde storniert und der Betrag erstattet.")
        y -= 54

    # ---- Parties -----------------------------------------------------------
    col2 = M + (right - M) / 2 + 8

    def label(x, yy, txt):
        c.setFillColorRGB(*_MUTE)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x, yy, txt.upper())

    def lines(x, yy, rows, lead=13):
        c.setFillColorRGB(*_INK)
        c.setFont("Helvetica", 10)
        for row in rows:
            if row:
                c.drawString(x, yy, row)
            yy -= lead
        return yy

    label(M, y, "Leistungserbringer · From")
    label(col2, y, "Empfänger · Billed to")
    y2 = y - 14
    recipient = [
        receipt.billing_name or receipt.student_name,
        receipt.billing_line1,
        (f"{receipt.billing_postcode} {receipt.billing_city}").strip(),
        receipt.billing_country,
    ]
    # Draw both columns and start the table below the taller one (smaller y), so a
    # short recipient block never lets the line item overlap the supplier address.
    y_supplier = lines(M, y2, [
        SUPPLIER["name"], SUPPLIER["line2"], SUPPLIER["addr"],
        SUPPLIER["city"], SUPPLIER["email"],
    ])
    y_recipient = lines(col2, y2, [r for r in recipient if r])
    y = min(y_supplier, y_recipient) - 10

    # ---- Line-item table ---------------------------------------------------
    y -= 6
    row_h = 20
    c_menge = M + 6
    c_desc = M + 62
    c_einzel = right - 150
    c_betrag = right - 8

    c.setFillColorRGB(0.96, 0.95, 0.92)
    c.rect(M, y - row_h + 4, right - M, row_h, stroke=0, fill=1)
    c.setFillColorRGB(*_MUTE)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(c_menge, y - 9, "MENGE")
    c.drawString(c_desc, y - 9, "BESCHREIBUNG")
    c.drawRightString(c_einzel, y - 9, "EINZEL")
    c.drawRightString(c_betrag, y - 9, "BETRAG")
    y -= row_h + 18  # breathing room between the header band and the line item

    c.setFillColorRGB(*_INK)
    c.setFont("Helvetica", 10)
    c.drawString(c_menge, y, str(credits))
    c.drawString(c_desc, y, "Einheiten für Englisch-Einzelunterricht")
    c.drawRightString(c_einzel, y, f"€ {_money(unit)}")
    c.drawRightString(c_betrag, y, f"€ {_money(net)}")
    c.setFillColorRGB(*_MUTE)
    c.setFont("Helvetica", 8)
    c.drawString(c_desc, y - 11, "1 Einheit = 45 Minuten Unterricht")
    y -= 24
    c.setStrokeColorRGB(*_LINE)
    c.line(M, y, right, y)
    y -= 22

    # ---- Totals ------------------------------------------------------------
    def total_row(yy, k, v, bold=False, big=False):
        c.setFillColorRGB(*_INK)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 12 if big else 10)
        c.drawRightString(right - 120, yy, k)
        c.drawRightString(right, yy, v)

    total_row(y, "Nettobetrag · Net", f"€ {_money(net)}")
    y -= 16
    total_row(y, "USt · VAT (0%)", "€ 0,00")
    y -= 8
    c.setStrokeColorRGB(*_LINE)
    c.line(right - 200, y, right, y)
    y -= 18
    total_row(y, "Gesamt · Total", f"€ {_money(net)}", bold=True, big=True)
    y -= 34

    # ---- VAT-exemption note ------------------------------------------------
    box_h = 44
    c.setFillColorRGB(0.945, 0.925, 0.878)
    c.setStrokeColorRGB(0.85, 0.83, 0.78)
    c.roundRect(M, y - box_h, right - M, box_h, 8, stroke=1, fill=1)
    c.setFillColorRGB(0.29, 0.25, 0.20)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(M + 12, y - 17, VAT_DE)
    c.setFont("Helvetica", 9)
    c.drawString(M + 12, y - 31, VAT_EN)

    # ---- Footer ------------------------------------------------------------
    c.setFillColorRGB(*_MUTE)
    c.setFont("Helvetica", 8.5)
    pay = "Storniert — Betrag erstattet" if is_storno else "Externe Überweisung — bezahlt"
    c.drawString(M, M, f"Zahlung · Payment: {pay}")
    c.drawRightString(right, M, "Automatisch ausgestellter Beleg")

    c.showPage()
    c.save()
    return buf.getvalue()
