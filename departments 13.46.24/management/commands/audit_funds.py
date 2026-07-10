"""Audit (and optionally repair) fund trust-classification consistency.

Two fields describe whether a fund is a trust fund:
  * fund_type  ("TRUST"/"LOCAL")  — authoritative; used by the reports, the
                                     balance engine and the general ledger.
  * is_trust   (cached boolean)   — used by the envelope summary and a few pickers.

They are kept in step by Department.save(), but a bulk update or an import that
bypasses save() can leave them disagreeing. When they disagree the reports and the
envelope summary show different trust totals and the reconciliation can't balance.

    python manage.py audit_funds                # report disagreements only
    python manage.py audit_funds --fix          # trust fund_type: set is_trust from it
    python manage.py audit_funds --from-cache   # trust is_trust: set fund_type from it

Use --from-cache when the *envelope summary* is the correct one (its trust funds are
right but the reports are wrong) — it makes Fund Type match what the summary shows.
Use --fix when the Fund Type settings are correct but a report/picker looks wrong.
After repairing, rebuild the general ledger so entries re-post under the corrected
classification.
"""
from django.core.management.base import BaseCommand

from departments.models import Department


class Command(BaseCommand):
    help = "Audit (and with --fix/--from-cache repair) fund trust classification."

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true",
                            help="Authoritative=Fund Type: set is_trust from fund_type.")
        parser.add_argument("--from-cache", action="store_true",
                            help="Authoritative=envelope summary: set fund_type from is_trust.")

    def handle(self, *args, **opts):
        depts = list(Department.objects.all())
        conflicts = [d for d in depts if d.is_trust != (d.fund_type == "TRUST")]

        self.stdout.write(f"Departments: {len(depts)}")
        self.stdout.write(f"Fund Type vs envelope-summary disagreements: {len(conflicts)}")
        for d in conflicts:
            shows = "Trust" if d.is_trust else "Local"
            says = "Trust" if d.fund_type == "TRUST" else "Local"
            self.stdout.write(f"  - {d.name}: Fund Type says {says}, "
                              f"envelope summary shows {shows}")

        if not conflicts:
            self.stdout.write(self.style.SUCCESS("All funds are consistent."))
            return

        if opts["fix"] and opts["from_cache"]:
            self.stderr.write("Choose only one of --fix or --from-cache.")
            return

        if opts["from_cache"]:
            n = 0
            for d in conflicts:
                Department.objects.filter(pk=d.pk).update(
                    fund_type=("TRUST" if d.is_trust else "LOCAL"))
                n += 1
            self.stdout.write(self.style.SUCCESS(
                f"Set Fund Type from the envelope summary for {n} fund(s). "
                f"Now rebuild the general ledger."))
        elif opts["fix"]:
            n = 0
            for d in conflicts:
                Department.objects.filter(pk=d.pk).update(
                    is_trust=(d.fund_type == "TRUST"))
                n += 1
            self.stdout.write(self.style.SUCCESS(
                f"Set is_trust from Fund Type for {n} fund(s). "
                f"Now rebuild the general ledger."))
        else:
            self.stdout.write(self.style.WARNING(
                "Report only. Re-run with --from-cache (trust the envelope summary) "
                "or --fix (trust the Fund Type settings) to repair."))
