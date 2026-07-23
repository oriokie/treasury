"""Report which assets are not backed by a ledger source.

Read-only. Run before switching register cost to acquisition-date temporal
costing:  python manage.py asset_preflight [--as-of YYYY-MM-DD]
"""
import datetime as dt

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Pre-flight check for acquisition-date temporal costing (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", dest="as_of", default=None,
                            help="Date to assess (default today).")

    def handle(self, *args, **opts):
        from core.metrics import metrics
        as_of = None
        if opts.get("as_of"):
            as_of = dt.date.fromisoformat(opts["as_of"])
        r = metrics.acquisition_coverage(as_of)
        t = r["totals"]
        self.stdout.write(f"Asset opening date: {r['opening_date']}   as at: {r['as_of']}")
        self.stdout.write(f"Assets acquired after the opening date: {t['count']}")
        self.stdout.write(f"  backed by the ledger : {t['covered']:>16,.2f}")
        self.stdout.write(f"  NOT backed           : {t['shortfall']:>16,.2f} "
                          f"({t['unbacked_count']} assets)")
        for row in r["unbacked"]:
            self.stdout.write(f"    - {row['asset'].name[:40]:<40} "
                              f"{row['acquired_on']}  short {row['shortfall']:,.2f}"
                              f"  - {row['reason']}")
        self.stdout.write(f"Payments that would be counted twice: "
                          f"{t['double_counted']:,.2f} ({t['double_count']} assets)")
        for row in r["double_counted"]:
            self.stdout.write(f"    - {row['asset'].name[:40]:<40} "
                              f"acquired {row['acquired_on']}  over {row['amount']:,.2f}")
        self.stdout.write("")
        self.stdout.write(f"Predicted register-vs-ledger cost difference after the "
                          f"change: {t['predicted_diff']:,.2f}")
        if r["ready"]:
            self.stdout.write(self.style.SUCCESS(
                "Nothing outstanding - cost can be recognised from each asset's "
                "acquisition date without the books disagreeing."))
        else:
            self.stdout.write(self.style.WARNING(
                f"Resolve {t['unbacked_count'] + t['double_count']} asset(s) first."))
