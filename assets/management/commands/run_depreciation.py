"""Generate and post a monthly depreciation run.

    python manage.py run_depreciation                # previous month
    python manage.py run_depreciation --year 2026 --month 7
    python manage.py run_depreciation --no-post      # generate a draft only

Idempotent for a draft month; refuses to touch a month already posted/locked.
Intended to be scheduled on the 1st of each month for the month just ended.
"""
import datetime as dt

from django.core.management.base import BaseCommand

from assets.services import runs


class Command(BaseCommand):
    help = "Generate (and post) the monthly asset depreciation run."

    def add_arguments(self, parser):
        today = dt.date.today()
        prev = (today.replace(day=1) - dt.timedelta(days=1))
        parser.add_argument("--year", type=int, default=prev.year)
        parser.add_argument("--month", type=int, default=prev.month)
        parser.add_argument("--no-post", action="store_true",
                            help="Generate the draft run without posting to the ledger.")

    def handle(self, *args, **opts):
        year, month = opts["year"], opts["month"]
        try:
            run = runs.generate_run(year, month)
        except ValueError as e:
            self.stderr.write(self.style.WARNING(str(e)))
            return
        msg = (f"Depreciation {year}-{month:02d}: {run.lines.count()} assets, "
               f"charge {run.total_charge}")
        if opts["no_post"]:
            self.stdout.write(self.style.SUCCESS(msg + " (draft, not posted)"))
            return
        runs.post_run(run)
        self.stdout.write(self.style.SUCCESS(msg + " — posted to ledger"))
