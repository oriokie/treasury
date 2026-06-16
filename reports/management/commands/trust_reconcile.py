"""Reconcile the two trust-fund totals for a month and itemise the difference.

    python manage.py trust_reconcile 2026 6

The Offering Summary sums *envelope lines* by the Sabbath they are counted under;
the Collections Summary sums *transactions* by their transaction date. Both use the
same is_trust classification, so any gap between them comes from one of a few
explainable places — this command shows which, with amounts, so you can tell a
genuine timing difference from data that needs fixing.
"""
import calendar
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum

from core.utils import sabbath_bucket
from envelopes.models import Envelope, EnvelopeLine
from giving.models import Transaction


class Command(BaseCommand):
    help = "Reconcile Offering-Summary trust vs Collections-Summary trust for a month."

    def add_arguments(self, parser):
        parser.add_argument("year", type=int)
        parser.add_argument("month", type=int)

    def handle(self, *args, **opts):
        y, m = opts["year"], opts["month"]
        if not 1 <= m <= 12:
            raise CommandError("Month must be 1–12.")
        last = calendar.monthrange(y, m)[1]

        def in_month(d):
            return d is not None and d.year == y and d.month == m

        # ---- Offering side: trust envelope lines bucketed by their Sabbath ----
        off_lines = (EnvelopeLine.objects
                     .filter(department__is_trust=True)
                     .select_related("envelope", "transaction", "department"))
        off_total = Decimal(0)
        no_txn = other_month_txn = excluded_txn = Decimal(0)
        for ln in off_lines:
            if not ln.envelope_id or sabbath_bucket(ln.envelope.date).month != m \
                    or sabbath_bucket(ln.envelope.date).year != y:
                continue
            off_total += ln.amount
            t = ln.transaction
            env = ln.envelope
            if t is None:
                # a line with no transaction is only truly missing from the ledger
                # if its envelope is NOT linked to a bank credit; if it is, that
                # bank credit is the ledger entry (and is in collections).
                if not env.bank_transaction_id:
                    no_txn += ln.amount                   # in offering, no ledger entry at all
            elif t.excluded_from_income:
                excluded_txn += ln.amount                 # in offering, excluded from collections
            elif not in_month(t.date):
                other_month_txn += ln.amount              # counted in collections of another month

        # ---- Collections side: trust transactions by transaction date ----
        coll = (Transaction.objects.confirmed_credits()
                .filter(excluded_from_income=False, department__is_trust=True,
                        date__year=y, date__month=m)
                .select_related("department"))
        coll_total = coll.aggregate(t=Sum("amount"))["t"] or Decimal(0)
        # trust collected this month whose Sabbath falls in another month (so it is
        # in another month's offering summary, not this one)
        coll_sabbath_other = Decimal(0)
        # trust collected this month with no envelope line (e.g. bank trust never
        # receipted as an envelope): in collections, not in the offering summary
        coll_no_line = Decimal(0)
        for t in coll:
            sb = t.service_sabbath or sabbath_bucket(t.date)
            if sb.year != y or sb.month != m:
                coll_sabbath_other += t.amount
            # a bank credit is "represented in the offering" if a line links to it
            # OR an envelope is linked to it as its bank deposit (env.bank_transaction)
            linked = (EnvelopeLine.objects.filter(transaction=t).exists()
                      or Envelope.objects.filter(bank_transaction=t).exists())
            if not linked:
                coll_no_line += t.amount

        w = self.stdout.write
        w(f"Trust reconciliation for {calendar.month_name[m]} {y}")
        w("=" * 52)
        w(f"Offering Summary trust (envelope lines, by Sabbath): {off_total:,.2f}")
        w(f"Collections Summary trust (transactions, by date):   {coll_total:,.2f}")
        w(f"Difference (offering − collections):                 {off_total - coll_total:,.2f}")
        w("")
        w("In the Offering Summary but NOT this month's Collections:")
        w(f"  envelope lines with no ledger transaction:         {no_txn:,.2f}")
        w(f"  lines whose transaction is excluded from income:   {excluded_txn:,.2f}")
        w(f"  lines whose transaction dates to another month:    {other_month_txn:,.2f}")
        w("In this month's Collections but NOT the Offering Summary:")
        w(f"  trust collected with no envelope line (e.g. bank):  {coll_no_line:,.2f}")
        w(f"  trust collected but counted on another month's Sabbath: {coll_sabbath_other:,.2f}")
        w("")
        if no_txn:
            w(self.style.WARNING(
                "→ Envelope lines with no transaction are the main thing to fix: a bank "
                "envelope was typed in without its bank transaction, so it shows in the "
                "offering summary but never reached the cash book / ledger."))
        else:
            w(self.style.SUCCESS(
                "→ No orphan envelope lines. Any remaining gap is a Sabbath/month-boundary "
                "timing difference (the same money counted in different months by the two "
                "reports), which is expected — not a classification error."))
