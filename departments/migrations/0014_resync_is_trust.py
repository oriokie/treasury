"""Re-sync the derived is_trust flag from the authoritative fund_type.

is_trust is a cached boolean set in Department.save(); if a fund's fund_type was
ever changed through a path that bypassed save() (a bulk .update(), a raw import),
the flag could go stale. A LOCAL fund left with is_trust=True would then wrongly
appear in the trust 'to remit' totals. This one-off repair brings every fund's
flag back in line with its fund_type.
"""
from django.db import migrations


def resync(apps, schema_editor):
    Department = apps.get_model("departments", "Department")
    # fund_type is the source of truth ("TRUST" vs "LOCAL")
    Department.objects.filter(fund_type="TRUST").exclude(is_trust=True).update(is_trust=True)
    Department.objects.exclude(fund_type="TRUST").filter(is_trust=True).update(is_trust=False)


class Migration(migrations.Migration):
    dependencies = [("departments", "0013_departmentleadership")]
    operations = [migrations.RunPython(resync, migrations.RunPython.noop)]
