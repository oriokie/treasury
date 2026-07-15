"""The register's own logic: import, running balance, and exception checking.

Reuses `statements.services.parser.read_rows()` — the same parser the ledger
importer uses, so the register reads a bank file exactly as the rest of the app
does. Nothing here posts, allocates, or touches a fund.
"""
import datetime as _dt
import re as _re
from decimal import Decimal

from django.db import models, transaction as db_tx
from django.db.models import Q, Sum
from django.utils import timezone

from statements.models_register import (RegisterException, StatementLine,
                                        StatementRegisterImport)


# ---------------------------------------------------------------------------
# Identity: what makes a bank line, and a transaction, the SAME thing
# ---------------------------------------------------------------------------

_MPESA_RECEIPT_RE = _re.compile(r"^(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{10}$")


def _is_mpesa_receipt(v):
    """A genuine 10-char M-Pesa receipt (letters AND digits) — the ONE handle on
    a payment that is unique per transaction, unlike a bank channel/batch ref."""
    return bool(v and _MPESA_RECEIPT_RE.match(v))


def dedup_key(row):
    """The bank's own unique identifier for a line.

    **Precedence puts the narration's M-Pesa receipt first.** A single bank
    channel or core-banking reference can legitimately be shared across several
    genuinely-distinct payments — a mobile-banking sweep that batches three
    contributions carries ONE `Channel REF` / `Core Ref` for all three, while
    each payment has its own unique 10-char M-Pesa receipt buried in the
    narration (e.g. UATKR5A7M8, UATKR5A7N9, UATKR5AIDQ under one
    SFI40DCBA1EA1F6DABA9). Keying on the channel/core ref first collapsed those
    three real payments into one and silently dropped two — money the register
    denied ever arrived. So when the parsed narration receipt IS a genuine
    M-Pesa receipt, it is the key; the channel and core refs are the fallback
    for lines that have no such receipt (cheques, bank charges).

    **A debit carries its direction in the key.** A bank reversing its own
    mistake issues the DEBIT under the SAME reference as the credit it is
    undoing — so keying purely on the reference deduplicated the reversal away
    as a "duplicate", losing the line entirely and leaving the register showing
    money the bank had already taken back.

    A line with none of these references — some banks emit a bare "monthly
    charge" row with no reference at all — gets a synthetic key built from the
    date, signed amount and narration. That is weaker (two identical charges on
    one day would collapse into one), and it is why `import_file()` reports such
    rows: a treasurer should know the register is doing its best rather than
    believing it is being exact.
    """
    is_debit = bool(row.get("debit"))

    # 1) the unique per-payment M-Pesa receipt from the narration wins outright
    rcpt = (row.get("receipt") or "").strip().upper()
    if _is_mpesa_receipt(rcpt):
        return (f"{rcpt}|D" if is_debit else rcpt)[:80]

    # 2) otherwise the bank's channel / core references, then the receipt column
    for key in ("mpesa_ref", "core_ref", "receipt"):
        v = (row.get(key) or "").strip().upper()
        if v:
            return (f"{v}|D" if is_debit else v)[:80]
    amt = (row.get("credit") or Decimal(0)) - (row.get("debit") or Decimal(0))
    narr = (row.get("raw_narration") or "")[:40]
    return f"SYN|{row['date']:%Y%m%d}|{amt}|{narr}"[:80]


_CHEQUE_RE = _re.compile(
    r"\b(?:CHQ|CHEQUE|CHK|CK)\b[.\s#:-]*(?:NO|NUM|NUMBER)?\b[.\s#:-]*0*(\d{3,10})\b",
    _re.I)
_BARE_NUMBER_RE = _re.compile(r"\b0*(\d{5,10})\b")


def cheque_number(line):
    """The cheque number a bank statement's debit narration refers to, or "".

    Banks write it several ways — "CHQ 000456", "CHEQUE NO. 456", "CHQ.456" —
    and pad it with zeros inconsistently between the statement and the cheque
    book. Leading zeros are stripped on both sides so "000456" and "456" are the
    same cheque, which they are.
    """
    text = (line.raw_narration or line.reference or "")
    m = _CHEQUE_RE.search(text)
    if m:
        return m.group(1).lstrip("0") or "0"
    return ""


def _instrument_keys(line):
    """Every payment-register key a statement DEBIT might correspond to.

    Chiefly the cheque number. Falls back to any bare 5-10 digit number in the
    narration, because some banks print the cheque number without saying that is
    what it is — and a number that long, on a debit, is nearly always an
    instrument number rather than an amount or a date.
    """
    out = set()
    n = cheque_number(line)
    if n:
        out.add(n)
        return out
    text = (line.raw_narration or line.reference or "")
    for m in _BARE_NUMBER_RE.finditer(text):
        out.add(m.group(1).lstrip("0") or "0")
    return out


_REVERSAL_WORDS = _re.compile(
    r"\b(REVERSAL|REVERSED|REVERSE|RVSL|RVRSL|CONTRA|"
    r"ERROR\s*CORRECTION|CORRECTION|WRONG\s*(?:CREDIT|DEBIT|POSTING)|"
    r"CANCELLED\s*TRANSACTION)\b", _re.I)


def looks_like_reversal(text):
    """Does this narration say the bank is undoing something?

    A keyword is REQUIRED — an amount-and-direction match alone is not enough to
    pair two entries as a reversal. A church that receives a 5,000 gift on Monday
    and pays a 5,000 supplier on Tuesday has two perfectly real movements, and
    silently erasing both because they happen to cancel out would be far worse
    than leaving a genuine reversal unrecognised.
    """
    return bool(_REVERSAL_WORDS.search(text or ""))


def _reversal_pairs(lines):
    """Pairs of statement lines where one reverses the other.

    A bank credits the church by mistake and takes it back; or debits in error
    and refunds it. The pair is a NON-EVENT: no money was really received or
    paid, and treating either half as real puts income in the books that never
    existed.

    Two lines pair when they are opposite in direction, equal in amount, within
    a few days of each other, and at least one of them SAYS it is a reversal —
    or they share a bank reference, which is the bank telling us the same thing
    more precisely.
    """
    by_amount = {}
    for ln in lines:
        by_amount.setdefault(abs(ln.signed_amount), []).append(ln)

    pairs = []
    used = set()
    for amount, group in by_amount.items():
        if amount == 0:
            continue
        for a in group:
            if a.pk in used:
                continue
            for b in group:
                if b.pk in used or b.pk == a.pk:
                    continue
                if (a.credit or 0) and not (b.debit or 0):
                    continue
                if (a.debit or 0) and not (b.credit or 0):
                    continue
                if abs((a.date - b.date).days) > 7:
                    continue
                # the reversal debit carries the same bank reference as the credit
                # it undoes — compare the reference itself, not the direction-tagged key
                same_ref = bool(a.dedup_key
                                and a.dedup_key.split("|")[0] == b.dedup_key.split("|")[0]
                                and not a.dedup_key.startswith("SYN|"))
                says_so = (looks_like_reversal(a.raw_narration)
                           or looks_like_reversal(b.raw_narration))
                if not (same_ref or says_so):
                    continue
                pairs.append((a, b))
                used.add(a.pk)
                used.add(b.pk)
                break
    return pairs


def _txn_keys(txn):
    """Every bank identifier a ledger transaction carries. A transaction and a
    statement line are the same event if they share ANY of these — because each
    of them is a value the BANK issued, and the bank does not issue the same
    receipt twice.

    Splits are the fiddly part, and there are two kinds:

      * Split through the UI (`Transaction.split_into`): parts share the
        parent's `mpesa_ref` and carry `REF-S1`-style core_refs, and each part
        points at the parent through `split_of`.
      * Split by the IMPORTER (a split-fund like Combined Offering): parts share
        the parent's `mpesa_ref` and carry `REF-S1` core_refs, but have NO
        `split_of` link — they are siblings created side by side, with no
        parent row at all.

    So both the "-S" suffix and the split_of link are followed. A part can also
    legitimately be zero-valued (a split fund with a 0% component), which is why
    nothing here looks at the amount: a zero-value part still carries the bank's
    reference, and still proves we recorded the line.
    """
    out = set()
    refs = [txn.mpesa_ref, txn.core_ref, txn.bank_receipt]
    parent = getattr(txn, "split_of", None)
    if parent is not None:
        refs += [parent.mpesa_ref, parent.core_ref, parent.bank_receipt]
    for v in refs:
        v = (v or "").strip().upper()
        if v:
            # a split part carries "XY9-S1"; the bank only ever knew "XY9"
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
    """What the account held immediately before `start`.

    Three sources, in order of how much they deserve to be believed:

    1. **The bank's own running balance on the last line before `start`.** The
       bank has told us; nothing we compute beats that.

    2. **The bank's own balance on the FIRST line the register holds, minus that
       line's own movement.** Even when the register starts mid-history, the very
       first line's balance column tells us what the account held immediately
       before it — so the opening is derivable, without anyone typing anything.
       This is the case that made a register starting in July show balances out
       by whatever was already in the account: it summed forward from zero
       instead of asking the bank.

    3. **`BankAccount.register_opening_balance`** — a figure a treasurer states,
       for statements that carry no balance column at all. Last, because a typed
       number is the only one of the three that can be wrong.
    """
    lines = StatementLine.objects.filter(account=account)

    if start:
        prior = (lines.filter(date__lt=start)
                 .order_by("-date", "-occurred_at", "-id").first())
        if prior is not None and prior.bank_balance is not None:
            return prior.bank_balance   # (1) the bank told us

    # (2) derive from the first line the register holds — the bank told us there too
    first = lines.order_by("date", "occurred_at", "id").first()
    base = Decimal(0)
    base_date = None
    if first is not None and first.bank_balance is not None:
        base = first.bank_balance - first.signed_amount
        base_date = first.date
    elif account.register_opening_balance is not None:
        # (3) the treasurer stated it
        base = account.register_opening_balance
        base_date = account.register_opening_date or (first.date if first else None)

    if not start or base_date is None:
        return base

    # accumulate everything between the base point and the window we are showing
    between = lines.filter(date__gte=base_date, date__lt=start)
    agg = between.aggregate(c=Sum("credit"), d=Sum("debit"))
    return base + (agg["c"] or Decimal(0)) - (agg["d"] or Decimal(0))


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

    # never REPORT outside what the register actually holds
    start = max(start, covered["start"]) if start else covered["start"]
    end = min(end, covered["end"]) if end else covered["end"]

    # --- the match index: EVERY transaction carrying a bank reference --------
    #
    # No channel filter. No account filter. No date filter. No amount filter.
    #
    # The question this side answers is narrow: "did we ever record this bank
    # line?" The only thing that can answer it is whether the bank's own
    # reference appears anywhere in our ledger. Everything else —
    # channel, bank_account, amount, excluded_from_income, is_reversed — is a
    # classification WE made after the fact, and every one of them turned out to
    # be capable of hiding a transaction that plainly carried the reference:
    #
    #   * Marking a gift as a manual receipt sets excluded_from_income and
    #     detaches it from its fund (giving.models.mark_manual_receipt). It is
    #     still the same bank line.
    #   * A split part can be zero-valued, and the importer creates split parts
    #     without a split_of link — they share the parent's mpesa_ref and carry
    #     "REF-S1" core_refs.
    #   * A transaction may carry no bank_account at all (a webhook entry), or
    #     one that was tagged later.
    #
    # A bank reference is unique forever and was issued by the BANK. If it is in
    # the ledger, we recorded that line — whatever we subsequently did to the
    # row. Reported: "I can get the references under M-Pesa ref in the
    # transactions. Yet being detected as missing." Exactly so, and this is why.
    #
    # The account and channel filters still belong on the OTHER direction below,
    # where the question really is about our own bank entries.
    ledger_by_key = {}
    for t in (Transaction.objects
              .exclude(Q(mpesa_ref="") & Q(core_ref__isnull=True)
                       & Q(bank_receipt__isnull=True))
              .select_related("split_of")
              .only("id", "date", "amount", "direction", "mpesa_ref",
                    "core_ref", "bank_receipt", "reference",
                    "split_of__mpesa_ref", "split_of__core_ref",
                    "split_of__bank_receipt")):
        for k in _txn_keys(t):
            ledger_by_key.setdefault(k, []).append(t)

    # The payments register: every cheque/EFT the church has issued, by its
    # number. This is what a statement DEBIT is matched against — a cheque
    # leaving the bank should correspond to a cheque we wrote.
    from cashbook.models import PaymentInstrument
    payment_keys = set()
    for num in (PaymentInstrument.objects
                .exclude(instrument_number="")
                .values_list("instrument_number", flat=True)):
        n = (num or "").strip().lstrip("0")
        if n:
            payment_keys.add(n)

    # every reference the register holds for this account, ever — same reasoning
    register_keys = set()
    for m, c, r in (StatementLine.objects.filter(account=account)
                    .values_list("mpesa_ref", "core_ref", "receipt")):
        for v in (m, c, r):
            if v:
                register_keys.add(v.upper())

    # What we REPORT on. Here the account and channel filters do belong: the
    # question is "which of OUR bank entries, on THIS account, has the bank
    # never mentioned?"
    account_txns = Transaction.objects.filter(
        channel=Transaction.Channel.BANK, is_reversal=False, is_reversed=False
    ).filter(Q(bank_account=account) | Q(bank_account__isnull=True))

    lines = StatementLine.objects.filter(account=account, date__gte=start, date__lte=end)
    txns = account_txns.filter(date__gte=start, date__lte=end)

    found, opened, closed = 0, 0, 0

    # A reversed pair is a NON-EVENT — the bank made an entry in error and undid
    # it. Neither half should be chased: there is nothing for our books to have
    # recorded, because nothing really happened. They stay in the register (the
    # bank did send them, and the register's whole contract is to say what the
    # bank said) and they net out in the running balance, exactly as they do on
    # the real statement.
    reversed_ids = set()
    for a, b in _reversal_pairs(list(lines)):
        reversed_ids.add(a.pk)
        reversed_ids.add(b.pk)

    # --- side 1: on the statement, not in our books -------------------------
    for ln in lines:
        if ln.pk in reversed_ids:
            closed += _close_if_open(account, RegisterException.Kind.MISSING_IN_LEDGER,
                                     line=ln)
            continue
        keys = {k.upper() for k in (ln.mpesa_ref, ln.core_ref, ln.receipt) if k}
        matched = any(k in ledger_by_key for k in keys)

        # A DEBIT usually carries no bank reference at all. M-Pesa gives every
        # CREDIT a receipt code, which is why the credit side worked from the
        # start — but the debits a church actually makes are cheques, standing
        # orders and bank charges, and the statement identifies those by a cheque
        # number in the narration, or by nothing whatever.
        #
        # So the whole debit side was falling through the "no reference, cannot
        # say" branch below and never being checked at all. That is the reported
        # bug, and it mattered: the credits are gifts arriving, which are pleasant
        # to get wrong; the debits are money LEAVING, which is not.
        if not matched and ln.debit:
            inst_keys = _instrument_keys(ln)
            if inst_keys & payment_keys:
                matched = True

        if matched:
            closed += _close_if_open(account, RegisterException.Kind.MISSING_IN_LEDGER,
                                     line=ln)
            found += 1
            continue

        if not keys and not ln.debit:
            # A CREDIT with no reference: we genuinely cannot say. It could be a
            # cash deposit somebody made at the counter. Saying nothing is more
            # honest than guessing.
            continue

        # A DEBIT reaches here whether or not it had a reference — and it is
        # flagged either way. Money left the account and our books do not know
        # about it; that is exactly what a treasurer needs to see, and staying
        # silent because the bank did not print a reference would hide the most
        # important thing this check can tell them.
        detail = (ln.raw_narration or ln.reference or "")[:255]
        if ln.debit and not keys:
            detail = (detail + "  [no bank reference — match this by hand]")[:255]
        _, created = _raise_exception(
            account, RegisterException.Kind.MISSING_IN_LEDGER, line=ln,
            date=ln.date, amount=ln.signed_amount,
            ref=(sorted(keys)[0] if keys else (cheque_number(ln) or "")),
            detail=detail)
        opened += int(created)

    # --- side 2: in our books, not on the statement -------------------------
    for t in txns:
        keys = _txn_keys(t)
        if not keys:
            # a bank-channel transaction with NO bank reference cannot be
            # checked against the bank at all. That is itself worth knowing,
            # but it is not a discrepancy — see `unverifiable()`.
            continue
        if keys & register_keys:
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
