"""Recompute where every member stands — arrears, grace, inactivity, exemptions.

Schedule nightly:

    python manage.py benevolent_automation

Safe to run repeatedly, safe to run unattended, and safe to run after a failure,
for a reason that is structural rather than careful: it writes ONLY to
`SchemeMembership.standing`, which is a cache of a pure function of the policy and
the facts. It does not write to `status` at all, so it is incapable of overruling a
treasurer's decision to suspend, withdraw or close a membership — not because it is
told not to, but because there is nowhere for it to write.

Recomputing a cache is also idempotent and free of consequence, which is why this
can be run as often as you like, in any order.

--dry-run reports what would change and changes nothing, which is how it should be
introduced to a live register.
"""
from django.core.management.base import BaseCommand

from benevolent.services import schemes as scheme_svc


class Command(BaseCommand):
    help = "Recompute benevolent membership standing (arrears, grace, inactivity)."

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
                f"{c['membership'].member.name}: {c['from'] or '—'} → {c['to']} "
                f"({c['reason']})")
        self.stdout.write(self.style.SUCCESS(prefix + result["summary"]))


class _Rollback(Exception):
    pass
