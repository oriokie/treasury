"""Phase 7 — honour any church's existing choice on the three old, narrowly-
named member-notification flags before removing them.

`notify_member_on_enrolment` / `_benefit_paid` / `_arrears` never actually
worked — the code that read them called the STAFF notification path
regardless (a confirmed bug: the docstring said "tell the member", the
implementation sent to treasurers). But a treasurer who set one of these to
True was expressing a genuine intent ("I want members told about X"), even
though the code never delivered on it. That intent is honoured here by
carrying a True value across to the new, complete `notify_member_*` fields
this phase introduces — the same courtesy Phase 3 and Phase 5 extended to
older fields they retired.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Settings = apps.get_model("benevolent", "BenevolentSettings")
    for cfg in Settings.objects.all().iterator():
        if getattr(cfg, "notify_member_on_enrolment", False):
            cfg.notify_member_registration = True
        if getattr(cfg, "notify_member_on_benefit_paid", False):
            cfg.notify_member_payout = True
        if getattr(cfg, "notify_member_on_arrears", False):
            cfg.notify_member_arrears_reminder = True
        cfg.save(update_fields=["notify_member_registration", "notify_member_payout",
                                "notify_member_arrears_reminder"])


def backwards(apps, schema_editor):
    pass  # the old fields are gone by the time this would run; nothing to restore


class Migration(migrations.Migration):

    dependencies = [
        ("benevolent", "0013_phase7_notifications"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
