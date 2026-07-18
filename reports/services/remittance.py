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
    """After a bulk .update() (which bypasses post_save signals), rebuild the
    general ledger so it always reflects batch approve/remit and stays
    reconciled."""
    try:
        from ledger.services import posting
        if posting.chart_ready():
            posting.rebuild()
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
