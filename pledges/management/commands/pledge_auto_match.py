"""Apply pledge auto-matches unattended — for cron.

Schedule (after enabling under Settings → Pledges → Allow scheduled auto-match):

    */30 * * * *  cd /path/to/treasury && .venv/bin/python manage.py pledge_auto_match

Safe to re-run: a second pass finds nothing new once gifts are linked.
By default only exact member / name-key matches are applied; pass --fuzzy to
also apply near-miss cash/envelope names (the same threshold as the preview).

--dry-run reports the plan and writes nothing.
"""
from django.core.management.base import BaseCommand
from decimal import Decimal

from core.models import SiteConfig
from pledges.models import PledgeCampaign
from pledges.services import matching as match_svc


class Command(BaseCommand):
    help = "Auto-match confirmed contributions to active pledges (for cron)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would be matched; write nothing.")
        parser.add_argument("--force", action="store_true",
                            help="Run even if scheduled auto-match is off in Settings.")
        parser.add_argument("--fuzzy", action="store_true",
                            help="Include fuzzy cash/envelope name matches "
                                 "(default: exact only).")
        parser.add_argument("--campaign", type=int, default=None,
                            help="Limit to one campaign id.")

    def handle(self, *args, **opts):
        cfg = SiteConfig.get()
        if not opts["force"] and not getattr(cfg, "pledge_cron_auto_match", False):
            self.stdout.write(self.style.WARNING(
                "Scheduled pledge auto-match is off. Turn it on under "
                "Settings → Pledges, or pass --force."))
            return

        campaign = None
        if opts["campaign"]:
            campaign = PledgeCampaign.objects.filter(pk=opts["campaign"]).first()
            if campaign is None:
                self.stderr.write(self.style.ERROR(
                    f"No campaign with id {opts['campaign']}."))
                return

        allow_fuzzy = bool(opts["fuzzy"])
        plan = match_svc.plan_auto_match_all(
            campaign=campaign, allow_fuzzy=allow_fuzzy, cfg=cfg)
        if not plan:
            self.stdout.write("No new matches found.")
            return

        total = sum((r["amount"] for r in plan), Decimal("0"))
        pledges = {r["pledge"].id for r in plan}
        prefix = "[dry run] " if opts["dry_run"] else ""
        for r in plan:
            tag = "fuzzy" if r.get("match") == "fuzzy" else "exact"
            self.stdout.write(
                f"{prefix}{r['pledge'].member.name} · {r['pledge'].campaign.name}: "
                f"KES {r['amount']:,.2f} from txn #{r['txn'].id} ({tag})")

        if opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS(
                f"[dry run] Would apply KES {total:,.2f} across "
                f"{len(pledges)} pledge(s), {len(plan)} link(s)."))
            return

        touched, applied = match_svc.apply_planned_matches(plan)
        self.stdout.write(self.style.SUCCESS(
            f"Auto-matched KES {applied:,.2f} across {touched} pledge(s)."))
