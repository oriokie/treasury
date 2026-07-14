"""Credits pending receipt — the data behind the transaction list's
"Pending receipt" quick filter, extracted into its own function so the web
export (Excel or PDF) and the Telegram bot route pull from exactly the same
query, never two slightly different ones.

Scope is BANK/M-Pesa credits into a RECEIPTABLE fund: every Trust fund **and the
whole Local Church Budget family** — the LCB funds a church has configured in
Settings, plus their subgroups.

**Cash is deliberately excluded.** A pending receipt is a gift that arrived
through the bank *without* a receipt and still needs one issued. Cash is
receipted at the point of counting — it goes onto an envelope at the table, it
does not arrive silently and wait to be chased. Listing it here asked a
treasurer to chase a receipt for money that was never going to have one.

That is the same "Trust + LCB" the Sabbath-confirm scope setting names, and it
is now literally the same code (`departments.models.receiptable_fund_ids`).
This list used to be Trust-only, so LCB money a church receipts exactly the way
it receipts trust money simply never appeared here — which is why it was called
"Trust pending receipt", a name that described the bug rather than the intent.
"""
from decimal import Decimal


def pending_receipt_rows():
    """[(date, phone, member_name, amount, fund_label, reference, mpesa_ref), ...]
    — receiptable-fund credits not yet formally receipted.

    "Receiptable" means Trust funds AND the LCB family, per
    `departments.models.receiptable_fund_ids()`. A split contribution qualifies
    if ANY of its parts landed in a receiptable fund — the whole gift is one
    receipt, so receipting half of it is not a thing.
    """
    from departments.models import receiptable_fund_ids
    from giving.models import Transaction
    from giving.views import _combined_fund_label, _group_split_siblings

    receiptable = receiptable_fund_ids()

    qs = (Transaction.objects.confirmed_credits()
          .filter(excluded_from_income=False,
                  channel=Transaction.Channel.BANK)
          .select_related("department", "member")
          .order_by("date", "id"))

    def _is_receipted(t):
        return (t.channel == Transaction.Channel.ENVELOPE
                or t.manual_receipt or t.processed_via_envelope)

    rows = []
    for members in _group_split_siblings(list(qs)):
        if not any(t.department_id in receiptable for t in members):
            continue
        if all(_is_receipted(t) for t in members):
            continue
        first = members[0]
        phone = first.payer_phone or (first.member.receipt_phone if first.member_id else "") or ""
        member_name = first.member.name if first.member_id else (first.payer_name or "")
        total = sum((t.amount for t in members), Decimal(0))
        mpesa_ref = next((t.mpesa_ref for t in members if t.mpesa_ref), "")
        rows.append((first.date, phone, member_name, total,
                     _combined_fund_label(members),
                     first.reference or first.mpesa_ref or first.core_ref or "", mpesa_ref))
    return rows


HEADER = ["Date", "Phone", "Member", "Amount", "Fund", "Reference", "M-Pesa Reference"]


def pending_receipt_pdf_bytes(church=""):
    """The same rows as an A4 PDF table — ReportLab, matching the style
    core.reporting.renderers.PdfRenderer already established for every
    other in-app PDF export (same fonts, same footer convention), rather
    than inventing a second PDF style for this one report."""
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    rows = pending_receipt_rows()
    buf = io.BytesIO()
    primary = colors.HexColor("#1f5f4f")
    brass = colors.HexColor("#b08d57")

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(14 * mm, 10 * mm, "Items pending receipt")
        canvas.drawRightString(landscape(A4)[0] - 14 * mm, 10 * mm, f"Page {doc.page}")
        if church:
            canvas.drawCentredString(landscape(A4)[0] / 2, 10 * mm, church)
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), topMargin=14 * mm,
                            bottomMargin=16 * mm, leftMargin=14 * mm, rightMargin=14 * mm)
    styles = getSampleStyleSheet()
    title_style = styles["Heading1"]
    title_style.textColor = primary
    flow = [Paragraph("Items pending receipt", title_style),
           Paragraph(f"{len(rows)} item(s) in a receiptable fund (Trust or Local Church Budget) not yet formally receipted.", styles["Normal"]),
           Spacer(1, 6 * mm)]

    table_data = [HEADER]
    total = Decimal(0)
    for date, phone, name, amount, fund, ref, mpesa in rows:
        total += amount
        table_data.append([date.strftime("%d %b %Y"), phone, name,
                           f"{amount:,.2f}", fund, ref, mpesa])
    table_data.append(["", "", "TOTAL", f"{total:,.2f}", "", "", ""])

    t = Table(table_data, repeatRows=1,
             colWidths=[22*mm, 28*mm, 45*mm, 24*mm, 35*mm, 30*mm, 30*mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), primary),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (3, 1), (3, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dddddd")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f7f5ef")]),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 1, brass),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flow.append(t)
    doc.build(flow, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
