"""Archive temporary allocation rules whose validity window ended a while ago.

Run nightly from cron, e.g.:
    python manage.py archive_expired_rules --grace 30

Rules are never deleted — archiving is a soft state that hides them from the
active list and stops them allocating new giving, while preserving the full
history for audit. A grace period keeps a just-expired rule visible briefly in
case it needs extending."""
import datetime as dt
from django.core.management.base import BaseCommand
from giving.models import AllocationRule


class Command(BaseCommand):
    help = "Soft-archive allocation rules expired beyond a grace period."

    def add_arguments(self, parser):
        parser.add_argument("--grace", type=int, default=30,
                            help="Days after expiry before archiving (default 30).")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        cutoff = dt.date.today() - dt.timedelta(days=opts["grace"])
        qs = AllocationRule.objects.filter(archived=False, valid_to__lt=cutoff)
        n = qs.count()
        if opts["dry_run"]:
            self.stdout.write(f"[dry-run] would archive {n} expired rule(s) "
                              f"(valid_to before {cutoff}).")
            return
        for r in qs:
            r.archive()
        self.stdout.write(self.style.SUCCESS(
            f"Archived {n} expired rule(s) (valid_to before {cutoff})."))
