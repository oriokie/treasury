"""Re-sync pledge payment links against the current state of each contribution.

A one-time repair for links that went stale before the app kept them in sync on
every change: money still counted on a pledge after its gift was reversed, or
after the gift was reallocated to a fund outside the campaign, or credited to a
different member. This sweep reads the same rule the live re-sync uses and drops
only the links that no longer hold. It never moves money and never creates new
matches (run ``pledge_auto_match`` for that).

    python manage.py resync_pledge_payments            # report + apply
    python manage.py resync_pledge_payments --dry-run   # report only
    python manage.py resync_pledge_payments --campaign 3
"""
from django.core.management.base import BaseCommand

from pledges.models import PledgeCampaign, PledgePayment
from pledges.services import matching as match_svc


class Command(BaseCommand):
    help = "Remove stale pledge payment links whose contribution has changed."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Show which links would be removed; write nothing.")
        parser.add_argument("--campaign", type=int, default=None,
                            help="Limit to one campaign id.")

    def handle(self, *args, **opts):
        campaign = None
        if opts["campaign"]:
            campaign = PledgeCampaign.objects.filter(pk=opts["campaign"]).first()
            if campaign is None:
                self.stderr.write(self.style.ERROR(
                    f"No campaign with id {opts['campaign']}."))
                return

        if opts["dry_run"]:
            # Evaluate without deleting: re-run the qualification for each linked
            # gift and report the ones that would be dropped.
            from core.models import SiteConfig
            from giving.models import Transaction
            cfg = SiteConfig.get()
            qs = (PledgePayment.objects.filter(transaction__isnull=False)
                  .select_related("transaction", "pledge", "pledge__member",
                                  "pledge__campaign"))
            if campaign is not None:
                qs = qs.filter(pledge__campaign=campaign)
            would_remove = 0
            for pp in qs:
                txn = pp.transaction
                hard = (txn.direction != Transaction.Direction.CREDIT
                        or not txn.confirmed or txn.is_reversal or txn.is_reversed)
                if hard:
                    keep = set()
                else:
                    keep = {p.id for p in match_svc.active_pledges_for_contribution(
                        txn, cfg, include_fulfilled=True)}
                drop = pp.pledge_id not in keep and (hard or txn.department_id is not None)
                if drop:
                    would_remove += 1
                    reason = ("reversed/unconfirmed" if hard
                              else "reallocated or moved to another member")
                    self.stdout.write(
                        f"[dry run] would remove KES {pp.amount:,.2f} from "
                        f"{pp.pledge.member.name} · {pp.pledge.campaign.name} "
                        f"(txn #{txn.id} — {reason})")
            self.stdout.write(self.style.SUCCESS(
                f"[dry run] {would_remove} link(s) would be removed."))
            return

        res = match_svc.resync_all_pledge_payments(campaign=campaign, rematch=False)
        self.stdout.write(self.style.SUCCESS(
            f"Checked {res['transactions_checked']} contribution(s); "
            f"removed {res['removed']} stale link(s)."))
