"""General Ledger health check — proactive integrity monitoring rather than
waiting to discover a problem during an audit. Every check here is read-only
and safe to run at any time; none of them modify data."""
from decimal import Decimal

from django.db.models import Sum, Count

from ledger.models import Account, JournalEntry, JournalLine


def unbalanced_journals():
    """Journal entries whose lines don't sum to debit == credit. Should always
    be empty — every posting path validates this before writing (see
    UnbalancedEntryError in posting.py) — but a health check should verify it
    directly against the database rather than only trust the code path that's
    supposed to prevent it, in case of a manual edit, a bad migration, or a
    future bug."""
    bad = []
    agg = (JournalLine.objects.values("entry_id")
           .annotate(d=Sum("debit"), c=Sum("credit"))
           .filter(**{}))
    for row in agg:
        d, c = row["d"] or Decimal(0), row["c"] or Decimal(0)
        if d != c:
            bad.append({"entry_id": row["entry_id"], "debit": d, "credit": c,
                       "diff": d - c})
    if bad:
        entries = {e.id: e for e in JournalEntry.objects.filter(
            id__in=[b["entry_id"] for b in bad])}
        for b in bad:
            b["entry"] = entries.get(b["entry_id"])
    return bad


def orphan_journals():
    """Journal entries whose source document no longer exists — the document
    was deleted through a path that didn't clean up its ledger postings (a
    hard delete outside the normal signal-driven flow, or leftover data from
    before a source document was removed some other way). Manual adjustment
    entries (source_type='manual') are excluded — they have no source
    document by design."""
    from giving.models import Transaction
    from cashbook.models import Expense, FundTransfer, ExpenseRefund
    lookups = {
        "transaction": Transaction, "expense": Expense,
        "transfer": FundTransfer, "refund": ExpenseRefund,
    }
    orphans = []
    for source_type, model in lookups.items():
        ids = set(JournalEntry.objects.filter(source_type=source_type)
                  .exclude(source_id=None).values_list("source_id", flat=True))
        if not ids:
            continue
        existing = set(model.objects.filter(pk__in=ids).values_list("pk", flat=True))
        missing = ids - existing
        if missing:
            orphans.extend(
                JournalEntry.objects.filter(source_type=source_type, source_id__in=missing)
                .prefetch_related("lines__account"))
    return orphans


def missing_source_documents():
    """Source documents that should have been posted (they meet post_*()'s own
    criteria) but have no journal entry at all — the opposite failure mode
    from an orphan: something that should be in the ledger silently isn't.
    A likely cause is data imported directly into the database, or a signal
    that didn't fire (e.g. a bulk .update()/.bulk_create() bypassing it)."""
    from giving.models import Transaction
    from cashbook.models import Expense
    missing = {"transactions": [], "expenses": []}
    posted_txn_ids = set(JournalEntry.objects.filter(source_type="transaction")
                         .values_list("source_id", flat=True))
    missing["transactions"] = list(
        Transaction.objects.filter(direction=Transaction.Direction.CREDIT, confirmed=True)
        # post_transaction() itself skips a reversed original or a reversal
        # contra-entry — by design, neither gets its own journal entry once
        # reversed — so without this exclusion these were flagged as
        # "missing" forever, surviving every rebuild, since rebuild()
        # correctly declines to post them too.
        .exclude(is_reversed=True).exclude(is_reversal=True)
        .exclude(pk__in=posted_txn_ids).select_related("department")[:200])
    posted_exp_ids = set(JournalEntry.objects.filter(source_type="expense")
                        .values_list("source_id", flat=True))
    missing["expenses"] = list(
        Expense.objects.filter(status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
        .exclude(pk__in=posted_exp_ids).select_related("department")[:200])
    return missing


def duplicate_postings():
    """More than one journal entry posted for the same source document — every
    posting function deletes its prior entry before writing a new one, so this
    should never happen; catches it anyway in case of a race condition, a
    direct database insert, or a future bug in a new posting path."""
    dupes = (JournalEntry.objects.exclude(source_type="manual")
             .exclude(source_id=None)
             .values("source_type", "source_id")
             .annotate(n=Count("id")).filter(n__gt=1).order_by("-n"))
    out = []
    for d in dupes:
        entries = list(JournalEntry.objects.filter(
            source_type=d["source_type"], source_id=d["source_id"]))
        out.append({"source_type": d["source_type"], "source_id": d["source_id"],
                    "count": d["n"], "entries": entries})
    return out


def duplicate_references():
    """Bank-sourced receipts sharing the same M-Pesa/bank receipt number or
    Core Reference. core_ref and bank_receipt are unique at the database
    level, so true duplicates can't exist there — this instead flags the
    softer signal of the same mpesa_ref appearing on more than one
    Transaction, which the schema does NOT prevent (a split gift legitimately
    reuses one mpesa_ref across several rows) so these are for a human to
    check, not necessarily errors."""
    import re
    from giving.models import Transaction
    dupes = (Transaction.objects.exclude(mpesa_ref="").exclude(mpesa_ref__isnull=True)
             .values("mpesa_ref").annotate(n=Count("id")).filter(n__gt=1).order_by("-n"))
    out = []
    for d in dupes[:100]:
        rows = list(Transaction.objects.filter(mpesa_ref=d["mpesa_ref"])
                    .select_related("department"))
        # Transaction.split_into() gives every sibling of one original
        # contribution its OWN core_ref — the base reference plus "-S1",
        # "-S2", etc. — never the same core_ref repeated. So a legitimate
        # split shows several *distinct* core_refs that all share the same
        # base (the part before "-S<number>"); only when the base itself
        # differs across rows is it a genuine, unexplained duplicate worth a
        # human's attention.
        bases = {re.sub(r"-S\d+$", "", r.core_ref) for r in rows if r.core_ref}
        out.append({"mpesa_ref": d["mpesa_ref"], "count": d["n"], "rows": rows,
                    "likely_split": len(bases) <= 1})
    return out


def funds_out_of_balance():
    """Per fund, the balance per the fund-report engine vs the balance purely
    from posted ledger lines — see ledger.services.posting.fund_balance_from_ledger.
    Any non-zero difference means the ledger and the reports it's supposed to
    tie to have drifted apart for that fund."""
    from departments.models import Department
    from reports.services import balances
    from ledger.services import posting
    eng = {r["department"].id: r for r in balances.department_summary(None, None, consolidated=False)}
    depts = list(Department.objects.filter(active=True))
    gl_balances = posting.fund_balances_from_ledger_bulk([d.id for d in depts])
    out = []
    for d in depts:
        engine_bal = eng.get(d.id, {}).get("closing", Decimal(0))
        gl_bal = gl_balances.get(d.id, Decimal(0))
        diff = engine_bal - gl_bal
        if diff != 0:
            out.append({"fund": d, "engine": engine_bal, "ledger": gl_bal, "diff": diff})
    return out


def run_health_check():
    """Everything in one call, for the admin dashboard."""
    from ledger.services import posting
    tb_rows, tb_totals = posting.trial_balance()
    eq = posting.accounting_equation()
    return {
        "trial_balance_rows": tb_rows,
        "trial_balance_totals": tb_totals,
        "trial_balance_balanced": tb_totals["debit"] == tb_totals["credit"],
        "accounting_equation": eq,
        "unbalanced_journals": unbalanced_journals(),
        "orphan_journals": orphan_journals(),
        "missing_source_documents": missing_source_documents(),
        "duplicate_postings": duplicate_postings(),
        "duplicate_references": duplicate_references(),
        "funds_out_of_balance": funds_out_of_balance(),
        "chart_ready": posting.chart_ready(),
    }
