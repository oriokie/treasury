"""The register's own logic: import, running balance, and exception checking.

Reuses `statements.services.parser.read_rows()` — the same parser the ledger
importer uses, so the register reads a bank file exactly as the rest of the app
does. Nothing here posts, allocates, or touches a fund.
"""
import datetime as _dt
from decimal import Decimal

from django.db import models, transaction as db_tx
from django.db.models import Q, Sum
from django.utils import timezone

from statements.models_register import (RegisterException, StatementLine,
                                        StatementRegisterImport)


# ---------------------------------------------------------------------------
# Identity: what makes a bank line, and a transaction, the SAME thing
# ---------------------------------------------------------------------------

def dedup_key(row):
    """The bank's own unique identifier for a line.

    Precedence: M-Pesa receipt, then core banking reference, then the plain
    receipt column. These are identifiers the BANK assigned; the church did not
    choose them and cannot collide with them.

    A line with none of the three — some banks emit a bare "monthly charge" row
    with no reference at all — gets a synthetic key built from the date, signed
    amount and narration. That is weaker (two identical charges on one day would
    collapse into one), and it is why `import_file()` reports such rows: a
    treasurer should know the register is doing its best rather than believing
    it is being exact.
    """
    for key in ("mpesa_ref", "core_ref", "receipt"):
        v = (row.get(key) or "").strip().upper()
        if v:
            return v[:80]
    amt = (row.get("credit") or Decimal(0)) - (row.get("debit") or Decimal(0))
    narr = (row.get("raw_narration") or "")[:40]
    return f"SYN|{row['date']:%Y%m%d}|{amt}|{narr}"[:80]


def _txn_keys(txn):
    """Every bank identifier a ledger transaction carries. A transaction and a
    statement line are the same event if they share ANY of these — because each
    of them is a value the BANK issued, and the bank does not issue the same
    receipt twice."""
    out = set()
    for v in (txn.mpesa_ref, txn.core_ref, txn.bank_receipt):
        v = (v or "").strip().upper()
        if v:
            # a split child carries "XY9-S1"; the bank only ever knew "XY9"
            out.add(v.split("-S")[0])
            out.add(v)
    return out


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

@db_tx.atomic
def import_file(account, *, path_or_bytes, filename, user, notes=""):
    """Read a bank file into the register. Additive and idempotent: a line the
    register already holds is skipped, so re-importing an overlapping period —
    or the same file twice — adds only what is genuinely new.

    This is what makes "import from January every month" a safe, sensible thing
    to do rather than a way to corrupt the record.
    """
    from statements.services.parser import read_rows

    rows = read_rows(path_or_bytes, filename)
    imp = StatementRegisterImport.objects.create(
        account=account, uploaded_by=user, filename=filename[:255],
        rows_read=len(rows), notes=notes)

    existing = set(
        StatementLine.objects.filter(account=account)
        .values_list("dedup_key", flat=True))

    added, dupes, failed = 0, 0, 0
    synthetic = 0
    dates = []
    to_create = []
    seen_this_file = set()

    for row in rows:
        try:
            key = dedup_key(row)
            if key.startswith("SYN|"):
                synthetic += 1
            if key in existing or key in seen_this_file:
                dupes += 1
                continue
            seen_this_file.add(key)
            to_create.append(StatementLine(
                account=account, imported_in=imp,
                date=row["date"], occurred_at=row.get("occurred_at"),
                credit=row.get("credit"), debit=row.get("debit"),
                bank_balance=row.get("balance"),
                core_ref=(row.get("core_ref") or "")[:64],
                mpesa_ref=(row.get("mpesa_ref") or "")[:64],
                receipt=(row.get("receipt") or "")[:64],
                reference=(row.get("reference") or "")[:120],
                payer_name=(row.get("name") or "")[:160],
                payer_phone=(row.get("phone") or "")[:32],
                raw_narration=row.get("raw_narration") or "",
                dedup_key=key))
            dates.append(row["date"])
            added += 1
        except Exception:  # noqa: BLE001 — one bad row must not sink the file
            failed += 1

    StatementLine.objects.bulk_create(to_create, batch_size=500)

    imp.lines_added = added
    imp.duplicates_skipped = dupes
    imp.rows_failed = failed
    if dates:
        imp.period_start, imp.period_end = min(dates), max(dates)
    if synthetic:
        imp.notes = (imp.notes + f"\n{synthetic} line(s) had no bank reference and were "
                     f"keyed on date+amount+narration — two identical such lines on one "
                     f"day would be treated as one.").strip()
    imp.save()
    return imp


# ---------------------------------------------------------------------------
# Running balance
# ---------------------------------------------------------------------------

def running(account, *, start=None, end=None, opening=None):
    """Every line in date order, each with OUR running balance beside the
    bank's own — so the two can be read against each other directly.

    `opening` is the balance carried into `start`. Where the statement itself
    tells us (a `bank_balance` on the line before the window), we use the bank's
    figure and say so, because the bank's assertion beats our arithmetic. Where
    it does not, we sum forward from the first line the register holds.
    """
    lines = StatementLine.objects.filter(account=account)
    if start:
        lines = lines.filter(date__gte=start)
    if end:
        lines = lines.filter(date__lte=end)
    lines = list(lines.order_by("date", "occurred_at", "id"))

    if opening is None:
        opening = _opening_before(account, start)

    bal = opening
    rows = []
    for ln in lines:
        bal += ln.signed_amount
        drift = None
        if ln.bank_balance is not None:
            drift = bal - ln.bank_balance
        rows.append({"line": ln, "running": bal, "drift": drift})
    return {"opening": opening, "rows": rows, "closing": bal}


def _opening_before(account, start):
    """What the account held immediately before `start`."""
    if not start:
        return Decimal(0)
    prior = (StatementLine.objects.filter(account=account, date__lt=start)
             .order_by("-date", "-occurred_at", "-id").first())
    if prior is not None and prior.bank_balance is not None:
        # the bank told us; believe the bank
        return prior.bank_balance
    agg = (StatementLine.objects.filter(account=account, date__lt=start)
           .aggregate(c=Sum("credit"), d=Sum("debit")))
    return (agg["c"] or Decimal(0)) - (agg["d"] or Decimal(0))


def balance_drift(account):
    """Where our arithmetic and the bank's own stated balance diverge.

    A non-zero drift on a line means: summing every line the register holds up
    to that point does not reach the balance the bank printed on that same line.
    That means the register is MISSING a line the bank included — the clearest
    possible signal that a statement period was never imported.
    """
    r = running(account)
    return [row for row in r["rows"]
            if row["drift"] is not None and row["drift"] != 0]


# ---------------------------------------------------------------------------
# Exceptions — where the bank's record and ours disagree
# ---------------------------------------------------------------------------

def coverage(account):
    """The date range the register actually holds lines for.

    Everything outside it is a blank the register cannot speak to — see
    `recheck()`, which will not assert a discrepancy about a period it has no
    bank data for.
    """
    agg = StatementLine.objects.filter(account=account).aggregate(
        s=models.Min("date"), e=models.Max("date"))
    return {"start": agg["s"], "end": agg["e"]}


@db_tx.atomic
def recheck(account, *, start=None, end=None):
    """Compare the register against the ledger and record the exceptions.

    **Bounded to the period the register actually covers.** This matters more
    than it sounds. If the register holds only July, it knows nothing about
    June — so comparing our June ledger against it would flag every June
    transaction as "missing from the bank", when in truth the bank simply has
    not been asked. That is not a discrepancy; it is an absence of evidence, and
    reporting it as one would bury the handful of real exceptions under hundreds
    of false ones, which is precisely how a discrepancy report gets ignored.

    So: outside the register's own date range, this check says nothing. Import
    the missing period and it will.

    Matching is by BANK REFERENCE only (M-Pesa receipt / core banking ref /
    bank receipt). Amount-and-date matching is deliberately NOT attempted: two
    members giving the same amount on the same day is completely ordinary, and
    a guess there would manufacture exactly the false reconciliation this check
    exists to prevent. A line the bank never gave a reference to simply cannot
    be matched with confidence, and saying so is more useful than pretending.

    Idempotent. An exception already RESOLVED or IGNORED stays that way: it is
    only touched to update `last_seen`. An exception that has since been
    explained by a newly-imported transaction is closed automatically.
    """
    from giving.models import Transaction

    covered = coverage(account)
    if covered["start"] is None:
        # an empty register can tell us nothing about anything
        return {"matched": 0, "opened": 0, "auto_closed": 0,
                "open": RegisterException.objects.filter(
                    account=account, status=RegisterException.Status.OPEN).count(),
                "covered_from": None, "covered_to": None}

    # never look outside what the register actually holds
    start = max(start, covered["start"]) if start else covered["start"]
    end = min(end, covered["end"]) if end else covered["end"]

    lines = StatementLine.objects.filter(account=account, date__gte=start, date__lte=end)
    txns = Transaction.objects.filter(
        channel=Transaction.Channel.BANK, is_reversal=False, is_reversed=False,
        date__gte=start, date__lte=end)

    # index the ledger by every bank reference it carries
    ledger_by_key = {}
    for t in txns.only("id", "date", "amount", "direction", "mpesa_ref",
                       "core_ref", "bank_receipt", "reference"):
        for k in _txn_keys(t):
            ledger_by_key.setdefault(k, []).append(t)

    line_keys = set()
    found, opened, closed = 0, 0, 0

    # --- side 1: on the statement, not in our books -------------------------
    for ln in lines:
        keys = {k for k in (ln.mpesa_ref, ln.core_ref, ln.receipt) if k}
        keys = {k.upper() for k in keys}
        line_keys |= keys
        matched = any(k in ledger_by_key for k in keys)
        if matched:
            closed += _close_if_open(account, RegisterException.Kind.MISSING_IN_LEDGER,
                                     line=ln)
            found += 1
            continue
        if not keys:
            # no bank reference at all — we cannot honestly assert either way
            continue
        _, created = _raise_exception(
            account, RegisterException.Kind.MISSING_IN_LEDGER, line=ln,
            date=ln.date, amount=ln.signed_amount, ref=sorted(keys)[0],
            detail=(ln.raw_narration or ln.reference or "")[:255])
        opened += int(created)

    # --- side 2: in our books, not on the statement -------------------------
    for t in txns:
        keys = _txn_keys(t)
        if not keys:
            # a bank-channel transaction with NO bank reference cannot be
            # checked against the bank at all. That is itself worth knowing,
            # but it is not a discrepancy — see `unverifiable()`.
            continue
        if keys & line_keys:
            closed += _close_if_open(account, RegisterException.Kind.MISSING_IN_BANK,
                                     transaction=t)
            continue
        amount = t.amount if t.direction == "CREDIT" else -t.amount
        _, created = _raise_exception(
            account, RegisterException.Kind.MISSING_IN_BANK, transaction=t,
            date=t.date, amount=amount, ref=sorted(keys)[0],
            detail=(t.reference or "")[:255])
        opened += int(created)

    open_now = RegisterException.objects.filter(
        account=account, status=RegisterException.Status.OPEN).count()
    return {"matched": found, "opened": opened, "auto_closed": closed,
            "open": open_now, "covered_from": start, "covered_to": end}


def _raise_exception(account, kind, *, line=None, transaction=None, **fields):
    lookup = dict(account=account, kind=kind)
    if line is not None:
        lookup["line"] = line
    else:
        lookup["transaction"] = transaction
    obj, created = RegisterException.objects.get_or_create(
        defaults={**fields, "status": RegisterException.Status.OPEN}, **lookup)
    if not created:
        # keep it fresh, but never re-open something a person has settled
        RegisterException.objects.filter(pk=obj.pk).update(last_seen=timezone.now())
    return obj, created


def _close_if_open(account, kind, *, line=None, transaction=None):
    """A previously-flagged item has since been explained by the other side —
    close it automatically, and say so, rather than making someone tick off an
    exception that has already resolved itself."""
    lookup = dict(account=account, kind=kind,
                  status=RegisterException.Status.OPEN)
    if line is not None:
        lookup["line"] = line
    else:
        lookup["transaction"] = transaction
    n = RegisterException.objects.filter(**lookup).update(
        status=RegisterException.Status.RESOLVED,
        resolved_at=timezone.now(),
        resolution="Matched automatically on a later re-check — the other side "
                   "of this entry has since been recorded.")
    return n


def unverifiable(account, *, start=None, end=None):
    """Bank-channel transactions carrying NO bank reference at all.

    Not a discrepancy — we cannot say the bank disagrees, only that we have no
    way to ask. Almost always a hand-entered transaction. Surfaced separately
    because calling it an exception would be an accusation the evidence does
    not support, and burying it would hide the one thing a treasurer can
    actually act on: give it a reference, or check it was really a bank
    payment.
    """
    from giving.models import Transaction
    # bounded to the register's coverage for the same reason recheck() is: a
    # bank transaction from a period we hold no statement for is not
    # "unverifiable", it is simply unasked
    covered = coverage(account)
    if covered["start"] is None:
        return Transaction.objects.none()
    start = max(start, covered["start"]) if start else covered["start"]
    end = min(end, covered["end"]) if end else covered["end"]
    qs = Transaction.objects.filter(
        channel=Transaction.Channel.BANK, is_reversal=False, is_reversed=False,
        date__gte=start, date__lte=end
    ).filter(
        Q(mpesa_ref="") | Q(mpesa_ref__isnull=True),
        Q(core_ref="") | Q(core_ref__isnull=True),
        Q(bank_receipt="") | Q(bank_receipt__isnull=True))
    return qs.select_related("department", "member").order_by("-date")


@db_tx.atomic
def resolve(exception, *, user, resolution, ignore=False):
    exception.status = (RegisterException.Status.IGNORED if ignore
                        else RegisterException.Status.RESOLVED)
    exception.resolved_by = user
    exception.resolved_at = timezone.now()
    exception.resolution = resolution[:255]
    exception.save(update_fields=["status", "resolved_by", "resolved_at", "resolution"])
    return exception


def summary(account):
    """The headline figures for the register's own page."""
    lines = StatementLine.objects.filter(account=account)
    agg = lines.aggregate(c=Sum("credit"), d=Sum("debit"))
    first = lines.order_by("date").values_list("date", flat=True).first()
    last = lines.order_by("-date").values_list("date", flat=True).first()
    exc = RegisterException.objects.filter(account=account,
                                           status=RegisterException.Status.OPEN)
    return {
        "lines": lines.count(),
        "credits": agg["c"] or Decimal(0),
        "debits": agg["d"] or Decimal(0),
        "balance": (agg["c"] or Decimal(0)) - (agg["d"] or Decimal(0)),
        "first_date": first,
        "last_date": last,
        "coverage": coverage(account),
        "open_exceptions": exc.count(),
        "missing_in_ledger": exc.filter(
            kind=RegisterException.Kind.MISSING_IN_LEDGER).count(),
        "missing_in_bank": exc.filter(
            kind=RegisterException.Kind.MISSING_IN_BANK).count(),
        "drift": balance_drift(account),
    }
