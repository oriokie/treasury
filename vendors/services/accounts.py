"""The supplier account — what we owe them, and everything we have done with them.

No arithmetic of its own. What the church owes on an obligation is decided by
``cashbook.services.treasury_position._open_obligation_total`` and by
``SettleableObligation.balance``; this module narrows those to one supplier and
arranges the result. If a figure here ever disagrees with the balance sheet, it
is because someone added a sum in this file, and they should not have.
"""
import datetime as _dt
from decimal import Decimal

from django.db import transaction as db_tx
from django.db.models import Count, Q, Sum

from ..models import Vendor, VendorNote


def _obligations(vendor):
    """This supplier's payables and accruals.

    Accruals reach a supplier only through the expenses that settled them —
    an accrual is an estimate of a cost, not of an invoice from a named
    supplier, so it has no supplier of its own and is not invented one here.
    """
    from cashbook.models import Payable
    return Payable.objects.filter(supplier=vendor)


def outstanding(vendor, as_of=None):
    """What is still owed to this supplier.

    Computed the same way the balance sheet computes it — per obligation,
    netted by payments made on or before the date, never negative — by calling
    the same helper. A supplier profile that added up its own invoices would be
    a second opinion on a liability.
    """
    # `balance_asof` IS the shared implementation — it lives on
    # SettleableObligation and is what the balance-sheet query reproduces in
    # SQL. Calling it per obligation here gives the same answer for one
    # supplier, without this module owning a rule about what counts as paid.
    return sum((p.balance_asof(as_of) for p in _obligations(vendor)),
               Decimal("0"))


def account_summary(vendor, as_of=None):
    """The headline figures on the supplier's profile."""
    from cashbook.models import Expense

    obligations = _obligations(vendor)
    open_ones = [p for p in obligations if not p.settled]
    today = as_of or _dt.date.today()

    owed = sum((p.balance_asof(as_of) for p in obligations), Decimal("0"))
    overdue = sum((p.balance_asof(as_of) for p in obligations
                   if p.due_date and p.due_date < today and not p.settled),
                  Decimal("0"))

    spend = (Expense.objects.filter(vendor=vendor,
                                    status__in=[Expense.Status.APPROVED,
                                                Expense.Status.PAID])
             .aggregate(t=Sum("amount"), n=Count("id")))

    return {
        "vendor": vendor,
        "outstanding": owed,
        "overdue": overdue,
        "open_count": len(open_ones),
        "total_spend": spend["t"] or Decimal("0"),
        "payment_count": spend["n"] or 0,
        "over_credit_limit": bool(
            vendor.credit_limit is not None and owed > vendor.credit_limit),
        "oldest_open": min((p.date for p in open_ones), default=None),
    }


def ageing(vendor, as_of=None):
    """What is owed, split by how long it has been owed.

    The ordinary 0-30 / 31-60 / 61-90 / 90+ buckets a treasurer expects, worked
    out from the due date where there is one and the invoice date where there is
    not — an invoice with no agreed terms is due when it arrives.
    """
    today = as_of or _dt.date.today()
    buckets = {"current": Decimal("0"), "d30": Decimal("0"),
               "d60": Decimal("0"), "d90": Decimal("0"), "older": Decimal("0")}
    for payable in _obligations(vendor):
        balance = payable.balance_asof(as_of)
        if balance <= 0:
            continue
        due = payable.due_date or payable.date
        days = (today - due).days
        if days <= 0:
            buckets["current"] += balance
        elif days <= 30:
            buckets["d30"] += balance
        elif days <= 60:
            buckets["d60"] += balance
        elif days <= 90:
            buckets["d90"] += balance
        else:
            buckets["older"] += balance
    buckets["total"] = sum(buckets.values(), Decimal("0"))
    return buckets


def transactions(vendor, limit=None):
    """Everything that has happened with this supplier, newest first.

    One list, not four tabs: an invoice raised, a payment made, an asset bought.
    A treasurer asking "what happened with Mwangi" wants it in order, not sorted
    by which table it lives in.
    """
    from cashbook.models import Expense

    rows = []
    for payable in _obligations(vendor).select_related("department"):
        rows.append({
            "date": payable.date, "kind": "Invoice",
            "description": payable.description,
            "fund": payable.department, "amount": payable.amount,
            "balance": payable.balance, "status": payable.status_label,
            "object": payable, "url_name": "payable_edit", "pk": payable.pk,
        })
    for expense in (Expense.objects.filter(vendor=vendor)
                    .select_related("department", "payable")
                    .order_by("-date")):
        rows.append({
            "date": expense.date, "kind": "Payment",
            "description": expense.description,
            "fund": expense.department, "amount": -expense.amount,
            "balance": None, "status": expense.get_status_display(),
            "object": expense, "url_name": "expense_list", "pk": expense.pk,
        })
    # Assets bought from this supplier. Included because "what have we bought
    # from them" is the question a treasurer asks before buying again, and an
    # asset purchase is the one that matters most.
    try:
        from assets.models import FixedAsset
        for asset in FixedAsset.objects.filter(supplier=vendor):
            rows.append({
                "date": asset.acquired_on, "kind": "Asset",
                "description": asset.name, "fund": None,
                "amount": asset.cost, "balance": None,
                "status": asset.get_status_display() if hasattr(asset, "get_status_display") else "",
                "object": asset, "url_name": "asset_list", "pk": asset.pk,
            })
    except Exception:
        pass

    rows.sort(key=lambda r: (r["date"] or _dt.date.min), reverse=True)
    return rows[:limit] if limit else rows


def timeline(vendor, limit=40):
    """A human-readable history of the supplier record itself.

    Built from `simple_history` rather than a second event table — the changes
    are already recorded, and a parallel log would be one more thing to keep
    truthful.
    """
    events = []
    for record in vendor.history.all()[:limit]:
        events.append({
            "at": record.history_date, "actor": record.history_user,
            "kind": {"+": "Created", "~": "Changed", "-": "Deleted"}.get(
                record.history_type, "Changed"),
            "summary": _describe(record),
        })
    for note in vendor.note_entries.all()[:limit]:
        events.append({"at": note.created_at, "actor": note.author,
                       "kind": "Note", "summary": note.body[:160]})
    for doc in vendor.documents.all()[:limit]:
        events.append({"at": doc.uploaded_at, "actor": doc.uploaded_by,
                       "kind": "Document",
                       "summary": f"{doc.get_kind_display()}: {doc}"})
    events.sort(key=lambda e: e["at"], reverse=True)
    return events[:limit]


def _describe(record):
    if record.history_type == "+":
        return f"Supplier record created as “{record.name}”."
    try:
        previous = record.prev_record
    except Exception:
        previous = None
    if previous is None:
        return "Record updated."
    changed = []
    for field in ("name", "status", "payment_terms", "phone", "email",
                  "tax_pin", "credit_limit"):
        if getattr(previous, field, None) != getattr(record, field, None):
            changed.append(field.replace("_", " "))
    return ("Changed " + ", ".join(changed)) if changed else "Record updated."


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@db_tx.atomic
def archive(vendor, *, user=None, reason=""):
    """Retire a supplier without losing the history.

    Archived, never deleted: every payable and expense that points here is
    evidence, and `on_delete=PROTECT` on those links means a delete would fail
    anyway. Archiving hides the supplier from pickers while leaving the record
    and its documents intact.
    """
    vendor.status = Vendor.Status.ARCHIVED
    vendor.archived_on = _dt.date.today()
    vendor.archived_reason = (reason or "")[:200]
    vendor.save(update_fields=["status", "archived_on", "archived_reason"])
    if reason:
        VendorNote.objects.create(vendor=vendor, author=user,
                                  body=f"Archived: {reason}")
    return vendor


@db_tx.atomic
def restore(vendor, *, user=None):
    vendor.status = Vendor.Status.ACTIVE
    vendor.archived_on = None
    vendor.archived_reason = ""
    vendor.save(update_fields=["status", "archived_on", "archived_reason"])
    return vendor


@db_tx.atomic
def merge(source, target, *, user=None):
    """Fold one supplier record into another.

    The same problem the member register solved with aliases: ten years of
    payables spelling one supplier three ways. Everything pointing at `source`
    is re-pointed at `target`, the source is archived rather than deleted (so
    the audit trail still resolves), and a note on the target records what was
    absorbed.
    """
    from cashbook.models import Expense, Payable

    if source.pk == target.pk:
        raise ValueError("A supplier cannot be merged into itself.")

    Payable.objects.filter(supplier=source).update(supplier=target)
    Expense.objects.filter(vendor=source).update(vendor=target)
    for related in (source.contacts.all(), source.addresses.all(),
                    source.bank_accounts.all(), source.documents.all(),
                    source.note_entries.all()):
        for row in related:
            row.vendor = target
            row.is_primary = getattr(row, "is_primary", False) and False
            row.save()

    VendorNote.objects.create(
        vendor=target, author=user,
        body=f"Absorbed the duplicate supplier record “{source.name}”.")
    archive(source, user=user, reason=f"Merged into “{target.name}”.")
    return target


def possible_duplicates(vendor=None):
    """Supplier records that look like the same business.

    Grouped on the normalised name key, which is what makes "Mwangi Hardware
    Ltd" and "MWANGI HARDWARE" land together.
    """
    qs = Vendor.objects.exclude(status=Vendor.Status.ARCHIVED)
    if vendor is not None:
        return qs.filter(name_key=vendor.name_key).exclude(pk=vendor.pk)
    dupes = (qs.values("name_key").annotate(n=Count("id"))
             .filter(n__gt=1).values_list("name_key", flat=True))
    return qs.filter(name_key__in=list(dupes)).order_by("name_key", "name")


def search(term, *, include_archived=False):
    """Find a supplier by any of the things a person might remember."""
    qs = Vendor.objects.all()
    if not include_archived:
        qs = qs.exclude(status=Vendor.Status.ARCHIVED)
    term = (term or "").strip()
    if not term:
        return qs
    from ..models import name_key as _key
    return qs.filter(
        Q(name__icontains=term) | Q(code__iexact=term)
        | Q(phone__icontains=term) | Q(email__icontains=term)
        | Q(tax_pin__iexact=term) | Q(name_key=_key(term))
        | Q(contacts__name__icontains=term)).distinct()
