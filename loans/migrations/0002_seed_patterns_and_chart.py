"""Seed the loan-module runtime data on deploy:

* the 'Loans payable' account (2300) in the chart of accounts, plus the loan
  expense-category accounts — via the existing ensure_chart(), which assigns
  expense codes by "next free code", never by list position;
* the default loan narration patterns (the aliases from the requirement),
  installed once and freely editable/deactivatable afterwards.

Both are idempotent, and skipped entirely on a fresh database where the chart
hasn't been initialised yet (ensure_chart runs on first ledger use instead).
"""
from django.db import migrations


def seed(apps, schema_editor):
    # chart: only extend an already-initialised chart (fresh installs get the
    # full chart, loans included, the first time the ledger is built)
    try:
        from ledger.services import posting
        if posting.chart_ready():
            posting.ensure_chart()
    except Exception:  # noqa: BLE001 — never block a migration on ledger state
        pass

    Pattern = apps.get_model("loans", "LoanNarrationPattern")
    from loans.services.narration import SEED_PATTERNS
    for pattern, kind in SEED_PATTERNS:
        Pattern.objects.get_or_create(
            pattern=pattern, kind=kind,
            defaults={"match_type": "CONTAINS", "seeded": True, "active": True})


def unseed(apps, schema_editor):
    Pattern = apps.get_model("loans", "LoanNarrationPattern")
    Pattern.objects.filter(seeded=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("loans", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
