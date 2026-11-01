"""Itemised campaign needs — import, remaining stock, pledge amount."""
from decimal import Decimal, InvalidOperation
import csv
import io
import re


def _dec(raw, default=None):
    if raw is None or raw == "":
        return default
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    s = str(raw).strip().replace(",", "").replace("KES", "").replace(" ", "")
    if not s:
        return default
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return default


def _norm_header(h):
    return re.sub(r"[^a-z0-9]+", "", (h or "").strip().lower())


#: Accepted column aliases from Camporee-style budgets and plain imports.
_NAME_KEYS = {"item", "itemdescription", "description", "name", "particulars"}
_QTY_KEYS = {"quantity", "qty", "needed", "quantityneeded", "units"}
_PRICE_KEYS = {"unitprice", "unitpricekes", "price", "rate", "unitcost"}
_UNIT_KEYS = {"unit", "uom", "measure"}


def _map_headers(headers):
    mapped = {}
    for i, h in enumerate(headers):
        key = _norm_header(h)
        if key in _NAME_KEYS and "name" not in mapped:
            mapped["name"] = i
        elif key in _QTY_KEYS and "qty" not in mapped:
            mapped["qty"] = i
        elif key in _PRICE_KEYS and "price" not in mapped:
            mapped["price"] = i
        elif key in _UNIT_KEYS and "unit" not in mapped:
            mapped["unit"] = i
    return mapped


def parse_items_file(uploaded):
    """Parse an uploaded CSV/XLSX into a list of item dicts.

    Returns (rows, error). Each row: name, quantity_needed, unit_price, unit.
    Skips blank lines and a trailing TOTAL row.
    """
    name = (getattr(uploaded, "name", "") or "").lower()
    raw = uploaded.read()
    if hasattr(uploaded, "seek"):
        uploaded.seek(0)

    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return _parse_xlsx(raw)
    return _parse_csv(raw)


def _parse_csv(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    return _rows_from_table(rows)


def _parse_xlsx(raw):
    try:
        import openpyxl
    except ImportError:
        return [], "Excel support is not available on this server."
    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    ws = wb.active
    rows = [[c if c is not None else "" for c in row]
            for row in ws.iter_rows(values_only=True)]
    return _rows_from_table(rows)


def _rows_from_table(table):
    if not table:
        return [], "The file is empty."
    # Find the header row: first row that maps to name + qty + price.
    header_i, cols = None, None
    for i, row in enumerate(table[:30]):
        mapped = _map_headers([str(c) for c in row])
        if {"name", "qty", "price"} <= set(mapped):
            header_i, cols = i, mapped
            break
    if cols is None:
        return [], ("Could not find columns for item name, quantity and unit "
                    "price. Use headers like: Item Description, Quantity, "
                    "Unit Price (KES).")

    out, errors = [], []
    for n, row in enumerate(table[header_i + 1:], start=header_i + 2):
        if not row or all(str(c).strip() == "" for c in row if c is not None):
            continue
        def cell(key):
            idx = cols.get(key)
            if idx is None or idx >= len(row):
                return ""
            return row[idx]

        name = str(cell("name") or "").strip()
        if not name:
            continue
        if _norm_header(name) in ("total", "totalkes", "grandtotal"):
            continue
        qty = _dec(cell("qty"))
        price = _dec(cell("price"))
        if qty is None or qty <= 0:
            errors.append(f"Row {n}: missing or invalid quantity for “{name}”.")
            continue
        if price is None or price < 0:
            errors.append(f"Row {n}: missing or invalid unit price for “{name}”.")
            continue
        unit = str(cell("unit") or "").strip()[:40]
        out.append({
            "name": name[:160],
            "quantity_needed": qty,
            "unit_price": price,
            "unit": unit,
        })
    if not out and not errors:
        return [], "No item rows found under the header."
    if errors and not out:
        return [], " ".join(errors[:3])
    return out, ""


def replace_campaign_items(campaign, rows, user=None):
    """Replace active items on ``campaign`` with ``rows`` from parse_items_file.

    Existing items that already have pledges are deactivated rather than
    deleted, so history stays intact. Returns (created, deactivated).
    """
    from pledges.models import PledgeCampaignItem
    existing = list(campaign.items.filter(active=True))
    deactivated = 0
    for item in existing:
        if item.pledges.exists():
            item.active = False
            item.save(update_fields=["active"])
            deactivated += 1
        else:
            item.delete()
    created = 0
    for i, row in enumerate(rows):
        PledgeCampaignItem.objects.create(
            campaign=campaign,
            name=row["name"],
            unit=row.get("unit") or "",
            quantity_needed=row["quantity_needed"],
            unit_price=row["unit_price"],
            sort_order=i * 10,
            active=True,
        )
        created += 1
    return created, deactivated


def pledge_amount_for(item, quantity):
    """KES amount for a quantity of ``item``."""
    qty = _dec(quantity, Decimal("0")) or Decimal("0")
    return (qty * (item.unit_price or Decimal("0"))).quantize(Decimal("0.01"))


def remaining_allows(item, quantity, exclude_pledge_id=None):
    """True when ``quantity`` fits in what is still needed."""
    qty = _dec(quantity, Decimal("0")) or Decimal("0")
    if qty <= 0:
        return False
    pledged = item.quantity_pledged(include_drafts=True)
    if exclude_pledge_id:
        from pledges.models import Pledge
        other = (Pledge.objects.filter(pk=exclude_pledge_id)
                 .values_list("item_quantity", flat=True).first())
        if other:
            pledged -= other
    return pledged + qty <= item.quantity_needed
