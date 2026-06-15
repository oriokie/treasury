"""Backfill member phone numbers from the transactions they're linked to.

Legacy bank giving stored the M-Pesa phone (parsed from the narration) on each
transaction's payer_phone, but earlier imports didn't copy it onto the member.
This command fills any blank member phone from a linked transaction, and links
orphan bank transactions (payer present, no member) to a matched/created member.
Safe to run repeatedly; reports what it changed. Use --dry-run to preview."""
from django.core.management.base import BaseCommand
from django.db.models import Q


class Command(BaseCommand):
    help = "Fill blank member phones from their transactions; link orphan contributions."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        from members.models import Member
        from members.services.matching import normalize_phone, match_or_create_member
        from giving.models import Transaction
        dry = opts["dry_run"]
        filled = linked = created = 0

        # 1) members with no phone -> take one from any linked transaction
        for m in Member.objects.filter(Q(phone__isnull=True) | Q(phone="")):
            tx = (Transaction.objects.filter(member=m)
                  .exclude(payer_phone="").exclude(payer_phone__isnull=True)
                  .first())
            if tx:
                ph = normalize_phone(tx.payer_phone)
                if ph:
                    if not dry:
                        m.phone = ph
                        m.save(update_fields=["phone"])
                    filled += 1

        # 2) orphan bank gifts (a payer name + phone but no member) -> match/create
        orphans = (Transaction.objects.filter(
            channel=Transaction.Channel.BANK, member__isnull=True)
            .exclude(payer_name=""))
        for tx in orphans.iterator():
            before = Member.objects.count()
            if dry:
                linked += 1
                continue
            member, how = match_or_create_member(tx.payer_name, tx.payer_phone)
            tx.member = member
            tx.save(update_fields=["member"])
            linked += 1
            if Member.objects.count() > before:
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f"{'[dry-run] ' if dry else ''}Filled {filled} member phone(s); "
            f"linked {linked} orphan contribution(s); created {created} member(s)."))
