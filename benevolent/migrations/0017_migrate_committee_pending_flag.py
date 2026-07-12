"""Phase 10 — carry forward any church's existing choice on the old,
wrongly-named notify_committee_on_pending_vote flag before removing it.

The old field was never actually reachable (its name broke the notify_on_*
convention _notify()/wants() depend on to look a toggle up), so no
deployment could have relied on it having any EFFECT — but a treasurer may
still have deliberately switched it off, and that choice deserves to survive
the rename rather than silently reset to the new field's default.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Settings = apps.get_model("benevolent", "BenevolentSettings")
    for cfg in Settings.objects.all().iterator():
        cfg.notify_on_committee_pending = cfg.notify_committee_on_pending_vote
        cfg.save(update_fields=["notify_on_committee_pending"])


def backwards(apps, schema_editor):
    Settings = apps.get_model("benevolent", "BenevolentSettings")
    for cfg in Settings.objects.all().iterator():
        cfg.notify_committee_on_pending_vote = cfg.notify_on_committee_pending
        cfg.save(update_fields=["notify_committee_on_pending_vote"])


class Migration(migrations.Migration):

    dependencies = [
        ("benevolent", "0016_phase10_rename_committee_pending_notice"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
