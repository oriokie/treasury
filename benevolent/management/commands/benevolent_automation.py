"""Apply the standing membership rules — arrears, inactivity, renewals.

Schedule nightly:

    python manage.py benevolent_automation

Safe to run repeatedly (it is idempotent), and safe to run unattended: it only
touches memberships in an automatable state, never one a human deliberately set,
and it reports every change it makes. --dry-run shows what it WOULD do without
touching anything, which is how it should be introduced to a live register.
"""
from django.core.management.base import BaseCommand

from benevolent.services import schemes as scheme_svc


class Command(BaseCommand):
    help = "Apply benevolent membership rules (arrears, inactivity, renewals)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change, and change nothing.")
        parser.add_argument("--force", action="store_true",
                            help="Run even if automation is switched off in the settings.")

    def handle(self, *args, **opts):
        if opts["dry_run"]:
            # a dry run must not persist anything, so it runs inside a rolled-back
            # transaction rather than in a separate, drift-prone 'preview' code path
            from django.db import transaction
            try:
                with transaction.atomic():
                    result = scheme_svc.run_automation(force=True)
                    self._report(result, dry=True)
                    raise _Rollback()
            except _Rollback:
                pass
            return

        result = scheme_svc.run_automation(force=opts["force"])
        if not result["ran"]:
            self.stdout.write(self.style.WARNING(result["reason"]))
            return
        self._report(result)

    def _report(self, result, dry=False):
        prefix = "[dry run] " if dry else ""
        for c in result["changes"]:
            self.stdout.write(
                f"{prefix}{c['scheme'].code} {c['membership'].number} "
                f"{c['membership'].member.name}: {c['from']} → {c['to']} ({c['reason']})")
        self.stdout.write(self.style.SUCCESS(prefix + result["summary"]))


class _Rollback(Exception):
    pass
