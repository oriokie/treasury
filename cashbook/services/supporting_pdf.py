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
