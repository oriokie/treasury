"""Create the missing ledger transaction for envelope lines that have none.

Older bank envelopes were recorded as envelope lines without a ledger
transaction, so the money shows in the offering summary but never reached the
cash book / collections (see `trust_reconcile`). This creates one ENVELOPE-channel
credit per orphan line, matching the envelope's date, fund, member and amount, and
links the line to it.

    python manage.py backfill_envelope_transactions          # report only
    python manage.py backfill_envelope_transactions --fix     # create + link

Run `trust_reconcile` first and confirm the orphan total matches; this only adds
money that is genuinely absent from the ledger. Rebuild the general ledger
afterwards so the new entries post.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction as db_tx
from django.db.models import Sum

from core.utils import sabbath_of
from envelopes.models import EnvelopeLine
from giving.models import Transaction


class Command(BaseCommand):
    help = "Create missing ledger transactions for orphan envelope lines."

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true",
                            help="Create and link the transactions (otherwise report only).")

    def handle(self, *args, **opts):
        orphans = (EnvelopeLine.objects.filter(transaction__isnull=True)
                   .select_related("envelope", "department", "envelope__member"))
        n = orphans.count()
        total = orphans.aggregate(t=Sum("amount"))["t"] or Decimal(0)
        trust_total = (orphans.filter(department__is_trust=True)
                       .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        self.stdout.write(f"Orphan envelope lines (no ledger transaction): {n}")
        self.stdout.write(f"  total amount: {total:,.2f}  (of which trust: {trust_total:,.2f})")
        if n == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to backfill."))
            return
        if not opts["fix"]:
            self.stdout.write(self.style.WARNING("Report only. Re-run with --fix to create them."))
            return

        created = 0
        with db_tx.atomic():
            for ln in orphans:
                env = ln.envelope
                if env is None:
                    continue
                d = env.date
                txn = Transaction.objects.create(
                    date=d, sabbath_week=getattr(env, "sabbath_week", None),
                    service_sabbath=sabbath_of(d),
                    channel=Transaction.Channel.ENVELOPE,
                    direction=Transaction.Direction.CREDIT, amount=ln.amount,
                    department=ln.department, dev_group=ln.dev_group,
                    member=env.member,
                    payer_name=env.contributor_name or "",
                    reference=f"envelope {env.receipt_no}",
                    allocation_status=Transaction.Status.MANUAL,
                    raw_narration=f"ENVELOPE {env.receipt_no} (backfilled)")
                ln.transaction = txn
                ln.save(update_fields=["transaction"])
                created += 1
        self.stdout.write(self.style.SUCCESS(
            f"Created and linked {created} transaction(s). Now rebuild the "
            f"general ledger (Ledger check -> Rebuild)."))
