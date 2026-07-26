"""Remittance dashboard helpers — days-outstanding, ledger repost after a bulk
update, and the per-fund collected/remitted/outstanding rows.

Pure logic, extracted verbatim from reports/views.py. Behaviour is unchanged;
reports/views.py re-exports these names (including the private `_days_outstanding`
and `_repost_to_ledger`, which giving/views.py imports) so every existing call
site keeps working exactly as before.
"""
import datetime as dt

from giving.models import Transaction
from . import balances


def days_outstanding(dept):
    """Days since the oldest *unremitted* trust receipt for a fund. Receipts up
    to the last remittance's period are already settled, so we count only from
    the first receipt after it — not the first contribution ever (which made
    everything look months overdue even right after remitting)."""
    from cashbook.models import RemittanceBatch
    last = (RemittanceBatch.objects.filter(
        status=RemittanceBatch.Status.REMITTED, period_end__isnull=False)
        .order_by("-period_end").first())
    q = Transaction.objects.filter(
        department=dept, direction=Transaction.Direction.CREDIT,
        confirmed=True, is_reversed=False, is_reversal=False)
    if last:
        q = q.filter(date__gt=last.period_end)
    first = q.order_by("date").first()
    if not first:
        return 0
    return (dt.date.today() - first.date).days


def repost_to_ledger(expenses=None):
    """After a bulk `.update()` (which bypasses post_save signals), bring the
    general ledger back in step so it always reflects batch approve/remit.

    Given the expenses that changed, only those are reposted. `post_expense`
    begins with `_replace_entries("expense", pk)`, so it is idempotent and also
    withdraws the entries when a status moves back out of APPROVED/PAID —
    reposting the affected rows is therefore complete, not a partial fix.

    This used to accept `expenses` and ignore it, calling `posting.rebuild()`
    instead: deleting every non-manual journal entry in the database and
    re-posting every transaction, expense, refund, transfer, asset acquisition,
    disposal and depreciation run the church has ever recorded — to approve one
    batch. On the seeded demo (214 transactions) that was 3,349 queries and 1.6
    seconds; on a real register with years of history it is minutes, twice per
    batch, which is what made approving a batch look like the page had hung.
    The work also grew every year the church kept using the system.

    Called with no argument it still rebuilds, so any caller that genuinely
    wants the whole ledger regenerated keeps that behaviour.
    """
    try:
        from ledger.services import posting
        if not posting.chart_ready():
            return
        if expenses is None:
            posting.rebuild()
            return
        for exp in expenses:
            posting.post_expense(exp)
    except Exception:
        from core.utils import log_exception as _lx
        _lx("reports/services/remittance.py")


def remittance_dashboard_rows(start=None, end=None):
    rows = []
    for r in balances.trust_summary(start, end):   # period/lifetime collected vs remitted
        out = r["to_remit"]
        rows.append({
            "department": r["department"], "collected": r["collected"],
            "remitted": r["remitted"], "outstanding": out,
            "unreceipted": r["unreceipted"],
            "days": days_outstanding(r["department"]) if out > 0 else 0,
        })
    return rows
