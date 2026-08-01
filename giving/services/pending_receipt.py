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
    — receiptable-fund credits not yet formally receipted, sorted by NAME
    (order-insensitively — "RUTH MOMANYI" and "MOMANYI RUTH" sort together, via
    `members.models.name_key`, the same key the rest of the system already uses
    to recognise the same person recorded two different ways) so the same
    giver's entries sit together wherever this list is read: the on-page view,
    the Excel and PDF downloads, and the PDF the Telegram bot sends. One
    function, one sort — nobody reading any of these four surfaces sees a
    different order or a different idea of "the same name" from the others.

    "Receiptable" means Trust funds AND the LCB family, per
    `departments.models.receiptable_fund_ids()`. A split contribution qualifies
    if ANY of its parts landed in a receiptable fund — the whole gift is one
    receipt, so receipting half of it is not a thing.
    """
    from departments.models import receiptable_fund_ids
    from giving.models import Transaction
    from giving.views import _combined_fund_label, _group_split_siblings
    from members.models import name_key

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

    # Sorted by the name as it is DISPLAYED, grouped by the matching key.
    #
    # Those are two different things and using one for both is what made this
    # list look unsorted. `name_key` sorts a name's words alphabetically so the
    # same person recorded "ALAN OTIENO" and "OTIENO ALAN" matches either way —
    # which is exactly right for grouping, and wrong for ordering: it files
    # "WIDOW NYAMONGO" under N while the Member column shows a W. A treasurer
    # working down the page to issue receipts then reads Z, A, W.
    #
    # So the key still decides who sits together, and the name on the page
    # decides where that block goes. A giver recorded under two spellings is
    # ordered by the earlier of them, so one block cannot land in two places.
    display_for_key = {}
    for row in rows:
        key = name_key(row[2]) or ""
        shown = (row[2] or "").strip().upper()
        if key and (key not in display_for_key or shown < display_for_key[key]):
            display_for_key[key] = shown

    def _sort_key(row):
        key = name_key(row[2]) or ""
        shown = display_for_key.get(key) or (row[2] or "").strip().upper()
        return (shown or "~", key, row[0])       # unnamed rows last

    rows.sort(key=_sort_key)
    return rows


def duplicate_name_flags(rows):
    """A list of booleans, one per row in `rows` (as returned by
    `pending_receipt_rows`), True wherever that row's name (by its
    order-insensitive key) appears on more than one row.

    Kept as ONE function so the on-page table, the Excel highlight and the PDF
    highlight can never disagree about what counts as a duplicate — each calls
    this rather than re-deriving its own notion of "the same name".
    """
    from collections import Counter
    from members.models import name_key

    keys = [name_key(r[2]) for r in rows]
    counts = Counter(k for k in keys if k)
    return [bool(k) and counts[k] > 1 for k in keys]


# The on-page table still shows Fund (you can sort by it there); the downloads
# deliberately do not. A pending-receipt sheet is worked through name by name
# to issue receipts, and the fund is decided by the receipt itself, so the
# column was a wide, rarely-read passenger on an already-wide landscape page.
HEADER = ["Date", "Phone", "Member", "Amount", "Reference", "M-Pesa Reference"]


def export_rows(rows):
    """`rows` reduced to the download columns (Fund dropped), in HEADER order.

    Shared by the Excel and PDF exports — and therefore by the PDF the Telegram
    bot's /pending sends — so the three can't drift into different columns.
    """
    return [[date, phone, name, amount, ref, mpesa]
            for (date, phone, name, amount, _fund, ref, mpesa) in rows]


def pending_receipt_pdf_bytes(church=""):
    """The same rows as an A4 PDF table — ReportLab, matching the style
    core.reporting.renderers.PdfRenderer already established for every
    other in-app PDF export (same fonts, same footer convention), rather
    than inventing a second PDF style for this one report.

    Sorted by name (see pending_receipt_rows). A name that repeats is shaded
    and set in bold — the highlight alone, with no "repeats" label, which is
    what was asked for. Bold is kept deliberately: this is a PDF people print,
    and a background tint can vanish on a black-and-white printer, so the row
    would otherwise lose its only marker. Bold survives greyscale without
    adding wording to the name.

    This is the exact PDF the Telegram bot's /pending command sends — one
    function, so the columns and the highlight are the same wherever it is
    read."""
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    rows = pending_receipt_rows()
    dup_flags = duplicate_name_flags(rows)
    buf = io.BytesIO()
    primary = colors.HexColor("#1f5f4f")
    brass = colors.HexColor("#b08d57")
    dup_fill = colors.HexColor("#f2e6d0")   # a light brass tint — visible in grayscale too

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
           Paragraph(f"{len(rows)} item(s) in a receiptable fund (Trust or Local Church Budget) not yet formally receipted.", styles["Normal"])]
    if any(dup_flags):
        from members.models import name_key
        n_dup = len({name_key(r[2]) for r, f in zip(rows, dup_flags) if f})
        flow.append(Paragraph(
            f"{n_dup} name(s) appear more than once — shaded and bold below.",
            styles["Normal"]))
    flow.append(Spacer(1, 6 * mm))

    table_data = [HEADER]
    total = Decimal(0)
    for (date, phone, name, amount, ref, mpesa) in export_rows(rows):
        total += amount
        table_data.append([date.strftime("%d %b %Y"), phone, name,
                           f"{amount:,.2f}", ref, mpesa])
    table_data.append(["", "", "TOTAL", f"{total:,.2f}", "", ""])

    # Fund is gone, so the remaining columns get its width back.
    t = Table(table_data, repeatRows=1,
             colWidths=[24*mm, 32*mm, 62*mm, 30*mm, 42*mm, 38*mm])
    style_cmds = [
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
    ]
    # duplicate-name rows override the zebra-stripe background (table row i is
    # data row i-1, since row 0 is the header) and set the name in bold, which
    # is the part that still reads once the tint is printed in greyscale
    for i, is_dup in enumerate(dup_flags, start=1):
        if is_dup:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), dup_fill))
            style_cmds.append(("FONTNAME", (2, i), (2, i), "Helvetica-Bold"))
    t.setStyle(TableStyle(style_cmds))
    flow.append(t)
    doc.build(flow, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
