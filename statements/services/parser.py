"""Statement parsing: read .csv/.xlsx/.xls into normalised rows and parse the
three narration shapes seen on the M-Pesa paybill statement.
"""
import csv
import datetime as dt
import io
import re

from dateutil import parser as dateparser

# --- column detection ------------------------------------------------------
# Map a normalised header token to the canonical field we want.
HEADER_HINTS = {
    "date": "date",
    "transactiondate": "date",
    "valuedate": "date",
    "postingdate": "date",
    "completiontime": "date",
    "narration": "narration",
    "description": "narration",
    "details": "narration",
    "particulars": "narration",
    "debit": "debit",
    "debitamount": "debit",
    "withdrawn": "debit",
    "credit": "credit",
    "creditamount": "credit",
    "paidin": "credit",
    "amount": "amount",
    "balance": "balance",
    "runningbalance": "balance",
    "coreref": "core_ref",
    "corereference": "core_ref",
    "transactionref": "core_ref",
    "reference": "core_ref",
    "channelref": "mpesa_ref",
    "mpesaref": "mpesa_ref",
    "receipt": "receipt",
    "receiptno": "receipt",
    "transactionid": "receipt",
}


def _norm_header(h):
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def _map_columns(headers):
    mapping = {}
    for idx, h in enumerate(headers):
        canon = HEADER_HINTS.get(_norm_header(h))
        if canon and canon not in mapping:
            mapping[canon] = idx
    return mapping


# --- narration parsing -----------------------------------------------------
PHONE_SEG = re.compile(r"(2547\d{8}|2541\d{8})")


def parse_narration(narration):
    """Return dict(receipt, reference, phone, name, shape) for the three shapes.

    Shape A (standard):  UER..~441211#tithe~254790301470~MPESAC2B_400222~KEVIN OGEGA
    Shape B ("Other"):   UER..~Other~254716804186~Development200
    Shape C (transfer):  AC0C40FD2E26 EDWIN ORIOKI KENYANSA Grp12dev
    """
    text = (narration or "").strip()
    out = {"receipt": "", "reference": "", "phone": "", "name": "", "shape": ""}
    if not text:
        return out

    if "~" in text:
        parts = [p.strip() for p in text.split("~")]
        out["receipt"] = parts[0]
        phone_match = PHONE_SEG.search(text)
        if phone_match:
            out["phone"] = phone_match.group(1)

        second = parts[1] if len(parts) > 1 else ""
        if "#" in second:
            # Shape A: paybill#reference
            out["reference"] = second.split("#", 1)[1]
            out["shape"] = "standard"
        elif second.lower() == "other":
            out["shape"] = "other"
            # last segment is a free-text hint, keep as candidate reference
            if parts[-1] and not PHONE_SEG.match(parts[-1]):
                out["reference"] = ""  # unknown -> queue; hint stays in raw_narration
        else:
            out["reference"] = second
            out["shape"] = "standard"

        # name = last segment that isn't the phone or a MPESAC2B marker
        for seg in reversed(parts):
            if seg and not PHONE_SEG.match(seg) and not seg.upper().startswith("MPESAC2B"):
                if seg != out["receipt"] and "#" not in seg and seg.lower() != "other":
                    out["name"] = seg
                    break
        return out

    # Shape C: bank transfer, space-separated, no '~'
    tokens = text.split()
    out["shape"] = "transfer"
    if tokens:
        out["receipt"] = tokens[0]
        # trailing token is a candidate reference (e.g. Grp12dev)
        if len(tokens) >= 2:
            out["reference"] = tokens[-1]
            out["name"] = " ".join(tokens[1:-1]) if len(tokens) > 2 else ""
    return out


# --- amount / date helpers -------------------------------------------------
def _to_decimal(raw):
    if raw is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(raw))
    if s in ("", "-", "."):
        return None
    try:
        from decimal import Decimal
        return Decimal(s)
    except Exception:
        return None


def _to_datetime(raw):
    """Return a datetime if the source cell carried a time, else None."""
    if isinstance(raw, dt.datetime):
        return raw
    if not raw:
        return None
    try:
        parsed = dateparser.parse(str(raw), dayfirst=True)
    except (ValueError, OverflowError):
        return None
    # only treat as a real time if it isn't exactly midnight (date-only sources)
    if parsed and (parsed.hour or parsed.minute or parsed.second):
        return parsed
    return None


def _to_date(raw):
    if isinstance(raw, (dt.date, dt.datetime)):
        return raw.date() if isinstance(raw, dt.datetime) else raw
    if not raw:
        return None
    try:
        return dateparser.parse(str(raw), dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


# --- readers ---------------------------------------------------------------
def _read_csv(content_bytes):
    text = content_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    return [row for row in reader if any(c.strip() for c in row)]


def _read_xlsx(path):
    import io
    import openpyxl
    src = io.BytesIO(bytes(path)) if isinstance(path, (bytes, bytearray)) else path
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for r in ws.iter_rows(values_only=True):
        if any(c is not None and str(c).strip() for c in r):
            rows.append(list(r))
    return rows


def _read_xls(path):
    import xlrd
    if isinstance(path, (bytes, bytearray)):
        book = xlrd.open_workbook(file_contents=bytes(path))
    else:
        book = xlrd.open_workbook(path)
    sheet = book.sheet_by_index(0)
    rows = []
    for i in range(sheet.nrows):
        rows.append(sheet.row_values(i))
    return rows


def _find_header_row(rows):
    """The statement may have title/preamble rows; find the row that maps best."""
    best_idx, best_map = 0, {}
    for i, row in enumerate(rows[:15]):
        mapping = _map_columns(row)
        if len(mapping) > len(best_map):
            best_idx, best_map = i, mapping
    return best_idx, best_map


def read_rows(path_or_bytes, filename):
    name = filename.lower()
    if name.endswith(".csv"):
        if isinstance(path_or_bytes, (bytes, bytearray)):
            raw = _read_csv(bytes(path_or_bytes))
        else:
            with open(path_or_bytes, "rb") as fh:
                raw = _read_csv(fh.read())
    elif name.endswith(".xlsx"):
        raw = _read_xlsx(path_or_bytes)
    elif name.endswith(".xls"):
        raw = _read_xls(path_or_bytes)
    else:
        raise ValueError("Unsupported file type. Use .csv, .xls or .xlsx.")

    if not raw:
        return []

    hdr_idx, mapping = _find_header_row(raw)
    if not mapping:
        raise ValueError("Could not detect statement columns from the header row.")

    parsed = []
    for row in raw[hdr_idx + 1:]:
        def cell(field):
            i = mapping.get(field)
            return row[i] if i is not None and i < len(row) else None

        narration = cell("narration") or ""
        credit = _to_decimal(cell("credit"))
        debit = _to_decimal(cell("debit"))
        amount = _to_decimal(cell("amount"))

        if credit is None and debit is None and amount is not None:
            # single signed amount column
            if amount < 0:
                debit, amount = abs(amount), None
            else:
                credit = amount

        if (credit is None or credit == 0) and (debit is None or debit == 0):
            continue  # nothing moved on this row

        date = _to_date(cell("date"))
        if not date:
            continue
        occurred_at = _to_datetime(cell("date"))

        narr = parse_narration(narration)
        # Some channels (STK push, USSD) put a placeholder like "STKPUSH" in the
        # M-Pesa / channel-ref column instead of the real receipt. The genuine
        # receipt (UF...) is the first segment of the narration. When the column
        # value is one of these placeholders, fall back to the narration receipt
        # so dedup keys stay unique and reconciliation ties out.
        _PLACEHOLDER_REFS = {"STKPUSH", "STK", "USSD", "C2B", "MPESAC2B",
                             "PAYBILL", "MULTI", "OTHER", ""}

        def _real(col_value):
            v = (str(col_value).strip() if col_value else "")
            return "" if v.upper() in _PLACEHOLDER_REFS else v

        mref = _real(cell("mpesa_ref")) or narr["receipt"]
        cref = _real(cell("core_ref")) or narr["receipt"]
        # M-Pesa receipts are canonically uppercase. Normalise the dedup keys to
        # uppercase so deduplication is exact regardless of the database's
        # collation (a case-insensitive collation like latin1_swedish_ci would
        # otherwise merge distinct receipts, or fold case unpredictably).
        cref = (cref or "").upper().strip() or None
        mref = (mref or "").upper().strip() or None
        rcpt = (narr["receipt"] or "").upper().strip()
        parsed.append({
            "date": date,
            "balance": _to_decimal(cell("balance")),   # running balance after this row
            "occurred_at": occurred_at,
            "credit": credit or None,
            "debit": debit or None,
            "core_ref": cref,
            "mpesa_ref": mref,
            "receipt": rcpt,
            "reference": narr["reference"],
            "phone": narr["phone"],
            "name": narr["name"],
            "raw_narration": str(narration).strip(),
            "shape": narr["shape"],
        })
    return parsed
