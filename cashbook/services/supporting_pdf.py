"""Build a single PDF of supporting documents for a set of expenses.

For each expense we emit a summary page (voucher, date, payee, amount, fund,
purpose) followed by that expense's attached documents. PDF attachments are
merged page-for-page; image attachments are drawn onto their own page; anything
missing or unsupported is noted and skipped so one bad file never stops the run.

reportlab + pypdf are optional at import time so the rest of the app keeps working
if they are not installed on the server; the view checks availability first.
"""
from __future__ import annotations
import io

try:                                    # optional deps
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from pypdf import PdfReader, PdfWriter
    HAVE_PDF_LIBS = True
except Exception:                        # noqa: BLE001
    HAVE_PDF_LIBS = False

PAGE_W, PAGE_H = (595.27, 841.89)        # A4 in points (fallback if reportlab absent)
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def _money(v):
    try:
        return f"KES {float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _wrap_lines(text, max_chars):
    text = str(text or "")
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [""]


def build_receipt_grid_pdf(attachments_by_month, church="", currency="KES",
                           cols=3, rows=3, max_attachments=600):
    """A compact, print-friendly PDF of receipt images laid out in a grid
    (several thumbnails per page, like a contact sheet) rather than one
    document per page — the shape a supporting-documents audit bundle needs,
    but not what a quick visual archive/filing catalog needs. Groups are
    separated by a light inline label rather than a forced page break, so a
    short month doesn't waste a page on its own.

    attachments_by_month: an OrderedDict of {"Month Year": [attachment, ...]}
    (the exact shape ReceiptArchiveView already builds for the HTML page).
    Non-image attachments (text notes, e-receipt links, unrecognised files)
    get a small placeholder cell with their label, so nothing is silently
    dropped from the printed archive.

    Returns (bytes, stats) — stats = {"documents": n, "images": n, "other": n}.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    margin = 12 * mm
    header_h = 16 * mm
    footer_h = 6 * mm
    grid_top = h - margin - header_h
    grid_bottom = margin + footer_h
    cell_w = (w - 2 * margin) / cols
    cell_h = (grid_top - grid_bottom) / rows
    pad = 3 * mm
    stats = {"documents": 0, "images": 0, "other": 0}

    def draw_header(period_label):
        c.setFont("Helvetica-Bold", 12)
        c.setFillGray(0.1)
        c.drawString(margin, h - margin - 4 * mm, "Expense Receipts")
        c.setFont("Helvetica", 8)
        c.setFillGray(0.4)
        sub = f"{church} · {period_label}" if church else period_label
        c.drawString(margin, h - margin - 9 * mm, sub)
        c.setStrokeGray(0.75)
        c.line(margin, grid_top, w - margin, grid_top)
        c.setFillGray(0)

    def draw_footer(page_no):
        c.setFont("Helvetica", 7)
        c.setFillGray(0.55)
        c.drawRightString(w - margin, margin - 2 * mm, f"Page {page_no}")
        c.setFillGray(0)

    period_label = ""
    page_no = 1
    col = row = 0
    draw_header(period_label)

    def new_page():
        nonlocal col, row, page_no
        draw_footer(page_no)
        c.showPage()
        page_no += 1
        col = row = 0
        draw_header(period_label)

    def cell_origin():
        x = margin + col * cell_w
        y = grid_top - (row + 1) * cell_h
        return x, y

    def advance_cell():
        nonlocal col, row
        col += 1
        if col >= cols:
            col = 0
            row += 1
            # deliberately lazy: don't start a new page here even if the grid
            # is now full — only do that right before the NEXT item actually
            # needs to be drawn (see ensure_room below). Otherwise placing the
            # very last item that exactly fills the grid triggers a pointless
            # blank trailing page.

    def ensure_room():
        if row >= rows:
            new_page()

    def draw_month_label(label):
        # a light inline separator, only consuming a fraction of one row's
        # height — never a forced page break, so a short month doesn't waste
        # a whole page of its own
        nonlocal col, row
        if col != 0:
            col = 0
            row += 1
        ensure_room()
        x = margin
        y = grid_top - row * cell_h - 4 * mm
        c.setFont("Helvetica-Bold", 9)
        c.setFillGray(0.2)
        c.drawString(x + pad, y, label)
        c.setStrokeGray(0.85)
        c.line(x + pad, y - 1.5 * mm, w - margin - pad, y - 1.5 * mm)
        c.setFillGray(0)
        # the label uses a slice of this row; shift down without wasting the
        # rest of the row for actual thumbnails
        nonlocal_shift = 7 * mm
        return nonlocal_shift

    label_shift = 0
    count = 0
    for month, docs in attachments_by_month.items():
        label_shift = draw_month_label(month)
        for a in docs:
            if count >= max_attachments:
                break
            count += 1
            ensure_room()
            x, y = cell_origin()
            inner_x, inner_y = x + pad, y + pad
            inner_w, inner_h = cell_w - 2 * pad, cell_h - 2 * pad - label_shift
            top_y = y + cell_h - pad - label_shift
            label_shift = 0   # only the first row after a month label is shifted

            drew_image = False
            f = getattr(a, "file", None)
            if f:
                name = (getattr(f, "name", "") or "").lower()
                if name.endswith(IMAGE_EXTS):
                    try:
                        with f.open("rb") as fh:
                            data = io.BytesIO(fh.read())
                        img = ImageReader(data)
                        iw, ih = img.getSize()
                        if iw and ih:
                            avail_w, avail_h = inner_w, inner_h - 9 * mm
                            scale = min(avail_w / iw, avail_h / ih, 1.0) \
                                if avail_w > 0 and avail_h > 0 else 0
                            dw, dh = iw * scale, ih * scale
                            ix = inner_x + (inner_w - dw) / 2
                            iy = top_y - dh
                            c.drawImage(img, ix, iy, dw, dh,
                                       preserveAspectRatio=True, mask="auto")
                            drew_image = True
                            stats["images"] += 1
                    except Exception:   # noqa: BLE001 — one bad file never stops the run
                        drew_image = False

            if not drew_image:
                c.setStrokeGray(0.85)
                c.rect(inner_x, top_y - (inner_h - 9 * mm), inner_w, inner_h - 9 * mm)
                c.setFont("Helvetica", 7)
                c.setFillGray(0.5)
                placeholder = ("No file — text/e-receipt note" if (a.text or a.link)
                              else "No document attached")
                c.drawCentredString(inner_x + inner_w / 2,
                                    top_y - (inner_h - 9 * mm) / 2, placeholder)
                c.setFillGray(0)
                stats["other"] += 1

            # caption
            exp = a.expense
            cap_y = y + pad + 7 * mm
            c.setFont("Helvetica-Bold", 6.5)
            c.setFillGray(0.15)
            c.drawString(inner_x, cap_y,
                        f"#{exp.id} · {exp.date:%d %b %y}"[:40])
            c.setFont("Helvetica", 6.5)
            c.setFillGray(0.35)
            dept = exp.department.name if exp.department_id else "-"
            c.drawString(inner_x, cap_y - 3 * mm, dept[:32])
            c.setFont("Helvetica-Bold", 7)
            c.setFillGray(0.05)
            c.drawString(inner_x, cap_y - 6.2 * mm,
                        f"{currency} {float(exp.amount):,.2f}")
            c.setFillGray(0)
            stats["documents"] += 1

            advance_cell()
        if count >= max_attachments:
            break

    draw_footer(page_no)
    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue(), stats


def _summary_pdf_bytes(exp, church=""):
    """A one-page cover sheet for a single expense."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = h - 30 * mm

    if church:
        c.setFont("Helvetica", 9)
        c.setFillGray(0.4)
        c.drawString(20 * mm, h - 15 * mm, church)
        c.setFillGray(0)

    c.setFont("Helvetica-Bold", 16)
    c.drawString(20 * mm, y, "Expense Voucher")
    y -= 6 * mm
    c.setStrokeGray(0.8)
    c.line(20 * mm, y, w - 20 * mm, y)
    y -= 12 * mm

    def field(label, value):
        nonlocal y
        c.setFont("Helvetica-Bold", 10)
        c.setFillGray(0.35)
        c.drawString(20 * mm, y, label)
        c.setFillGray(0)
        c.setFont("Helvetica", 12)
        # wrap long values simply
        text = str(value or "-")
        max_chars = 70
        lines = [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or ["-"]
        for i, ln in enumerate(lines):
            c.drawString(60 * mm, y - (i * 6 * mm), ln)
        y -= max(9 * mm, len(lines) * 6 * mm + 3 * mm)

    field("Voucher no.", exp.voucher_no or f"EXP-{exp.id}")
    field("Date", exp.date.strftime("%d %B %Y") if exp.date else "-")
    field("Payee", exp.claimant or "-")
    field("Amount", _money(exp.amount))
    field("Fund", exp.department.name if exp.department_id else "-")
    field("Category", exp.get_category_display())
    field("Status", exp.get_status_display())
    field("Purpose", exp.description or "-")

    n = exp.attachments.count() if hasattr(exp, "attachments") else 0
    c.setFont("Helvetica-Oblique", 9)
    c.setFillGray(0.45)
    c.drawString(20 * mm, 20 * mm,
                 f"{n} supporting document(s) follow." if n else
                 "No supporting documents were attached to this expense.")
    c.showPage()
    c.save()
    buf.seek(0)
    return buf


def _image_pdf_bytes(fileobj):
    """Draw one image centred on an A4 page. Returns bytes or None on failure."""
    try:
        img = ImageReader(fileobj)
        iw, ih = img.getSize()
        if not iw or not ih:
            return None
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        w, h = A4
        margin = 15 * mm
        avail_w, avail_h = w - 2 * margin, h - 2 * margin
        scale = min(avail_w / iw, avail_h / ih)
        dw, dh = iw * scale, ih * scale
        c.drawImage(img, (w - dw) / 2, (h - dh) / 2, dw, dh,
                    preserveAspectRatio=True, mask="auto")
        c.showPage()
        c.save()
        buf.seek(0)
        return buf
    except Exception:                    # noqa: BLE001
        return None


def _note_pdf_bytes(message):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Oblique", 11)
    c.setFillGray(0.4)
    c.drawString(20 * mm, h - 40 * mm, message[:110])
    c.showPage()
    c.save()
    buf.seek(0)
    return buf


def _append(writer, pdf_bytes):
    try:
        reader = PdfReader(pdf_bytes)
        for page in reader.pages:
            writer.add_page(page)
        return True
    except Exception:                    # noqa: BLE001
        return False


def build_supporting_docs_pdf(expenses, church="", max_expenses=400):
    """Return (bytes, stats) for a merged supporting-documents PDF.

    stats = {"expenses": n, "attachments": n, "skipped": n}
    """
    writer = PdfWriter()
    stats = {"expenses": 0, "attachments": 0, "skipped": 0}

    for exp in expenses[:max_expenses]:
        stats["expenses"] += 1
        _append(writer, _summary_pdf_bytes(exp, church=church))

        atts = exp.attachments.all() if hasattr(exp, "attachments") else []
        for att in atts:
            f = getattr(att, "file", None)
            if not f:
                # text/link-only attachment: record it as a note page
                note = (att.text or att.link or "").strip()
                if note:
                    _append(writer, _note_pdf_bytes(f"Attachment (no file): {note}"))
                    stats["attachments"] += 1
                continue
            name = (getattr(f, "name", "") or "").lower()
            try:
                if name.endswith(".pdf"):
                    with f.open("rb") as fh:
                        data = io.BytesIO(fh.read())
                    if _append(writer, data):
                        stats["attachments"] += 1
                    else:
                        stats["skipped"] += 1
                        _append(writer, _note_pdf_bytes(
                            f"Could not read PDF attachment: {f.name}"))
                elif name.endswith(IMAGE_EXTS):
                    with f.open("rb") as fh:
                        data = io.BytesIO(fh.read())
                    page = _image_pdf_bytes(data)
                    if page and _append(writer, page):
                        stats["attachments"] += 1
                    else:
                        stats["skipped"] += 1
                        _append(writer, _note_pdf_bytes(
                            f"Unsupported or unreadable image: {f.name}"))
                else:
                    stats["skipped"] += 1
                    _append(writer, _note_pdf_bytes(
                        f"Unsupported attachment type: {f.name}"))
            except Exception:            # noqa: BLE001 — never let one file stop the run
                stats["skipped"] += 1
                _append(writer, _note_pdf_bytes(
                    f"Attachment could not be processed: {getattr(f, 'name', '?')}"))

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.getvalue(), stats
