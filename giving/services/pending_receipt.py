"""Trust-fund-credits-pending-receipt — the data behind the transaction
list's "Trust pending receipt" quick filter, extracted into its own
function so the web export (Excel or PDF) and the Telegram bot route pull
from exactly the same query, never two slightly different ones.
"""
from decimal import Decimal


def pending_receipt_rows():
    """[(date_iso, phone, member_name, amount, fund_label, reference,
    mpesa_ref), ...] — trust fund credits not yet formally receipted. See
    giving.views._trust_pending_receipt_export's own docstring for exactly
    what "pending" and "combined" mean here; this function is that same
    logic, just callable from more than one place now.
    """
    from departments.models import Department
    from giving.models import Transaction
    from giving.views import _combined_fund_label, _group_split_siblings

    qs = (Transaction.objects.confirmed_credits()
          .filter(excluded_from_income=False)
          .select_related("department", "member")
          .order_by("date", "id"))

    def _is_receipted(t):
        return (t.channel == Transaction.Channel.ENVELOPE
                or t.manual_receipt or t.processed_via_envelope)

    rows = []
    for members in _group_split_siblings(list(qs)):
        has_trust = any(t.department_id and t.department.fund_type == Department.FundType.TRUST
                        for t in members)
        if not has_trust:
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
        canvas.drawString(14 * mm, 10 * mm, "Trust fund items pending receipt")
        canvas.drawRightString(landscape(A4)[0] - 14 * mm, 10 * mm, f"Page {doc.page}")
        if church:
            canvas.drawCentredString(landscape(A4)[0] / 2, 10 * mm, church)
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), topMargin=14 * mm,
                            bottomMargin=16 * mm, leftMargin=14 * mm, rightMargin=14 * mm)
    styles = getSampleStyleSheet()
    title_style = styles["Heading1"]
    title_style.textColor = primary
    flow = [Paragraph("Trust fund items pending receipt", title_style),
           Paragraph(f"{len(rows)} item(s) not yet formally receipted.", styles["Normal"]),
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
