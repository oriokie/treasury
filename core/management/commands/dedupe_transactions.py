"""Find and remove duplicate bank transactions that share an M-Pesa receipt.

Earlier imports could create two rows for the same payment when one carried a
core_ref and the other didn't (e.g. a STKPUSH placeholder vs the real bank
reference). The unique constraint is on core_ref/bank_receipt, not mpesa_ref,
so those slipped through. This command groups by mpesa_ref, keeps the best row
in each group, repoints any envelopes/expenses onto the kept row, and deletes
the rest. Always shows what it will do; use --apply to actually delete.

    python manage.py dedupe_transactions            # dry-run (default)
    python manage.py dedupe_transactions --apply     # perform the cleanup
"""
from django.core.management.base import BaseCommand
from django.db import transaction as db_tx
from django.db.models import Count


class Command(BaseCommand):
    help = "Remove duplicate transactions sharing an M-Pesa receipt."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually delete duplicates (default is dry-run).")

    def _score(self, t):
        """Higher = better record to KEEP. Prefer: has core_ref, allocated to a
        fund, confirmed, not a reversal, already receipted, lower id (older)."""
        s = 0
        if t.core_ref:
            s += 8
        if t.department_id:
            s += 4
        if t.confirmed:
            s += 2
        if t.processed_via_envelope:
            s += 2
        if t.is_reversal or t.is_reversed:
            s -= 8
        return s

    def handle(self, *args, **opts):
        from giving.models import Transaction
        from envelopes.models import Envelope, EnvelopeLine
        from cashbook.models import Expense

        apply = opts["apply"]
        groups = (Transaction.objects.exclude(mpesa_ref="")
                  .exclude(mpesa_ref__isnull=True)
                  .values("mpesa_ref").annotate(n=Count("id")).filter(n__gt=1))

        total_groups = groups.count()
        removed = 0
        self.stdout.write(f"Found {total_groups} M-Pesa ref(s) with more than one record.")

        for g in groups:
            txns = list(Transaction.objects.filter(mpesa_ref=g["mpesa_ref"])
                        .order_by("id"))
            # only treat as duplicate if amounts match (a true double of the same
            # payment). Different amounts on one ref are left for manual review.
            amounts = {t.amount for t in txns}
            if len(amounts) != 1:
                self.stdout.write(self.style.WARNING(
                    f"  {g['mpesa_ref']}: differing amounts {sorted(amounts)} — "
                    f"skipped (review manually)."))
                continue
            txns.sort(key=lambda t: (-self._score(t), t.id))
            keep, dupes = txns[0], txns[1:]
            self.stdout.write(
                f"  {g['mpesa_ref']}: keep id={keep.id} "
                f"(core_ref={keep.core_ref}), remove {[d.id for d in dupes]}")
            if not apply:
                removed += len(dupes)
                continue
            with db_tx.atomic():
                for d in dupes:
                    # repoint anything pointing at the duplicate onto the kept row
                    Envelope.objects.filter(bank_transaction=d).update(bank_transaction=keep)
                    EnvelopeLine.objects.filter(transaction=d).update(transaction=keep)
                    Expense.objects.filter(bank_transaction=d).update(bank_transaction=keep)
                    d.delete()
                    removed += 1

        verb = "Removed" if apply else "Would remove"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb} {removed} duplicate transaction(s) across {total_groups} ref(s)."
            + ("" if apply else "\nRe-run with --apply to perform the cleanup.")))
