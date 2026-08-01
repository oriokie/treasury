"""Repair the member-facing request email, in place, without touching edits.

The stored template read:

    Your {kind} — "{subject}" — reference {reference}, {phrase}.

`{phrase}` is supplied by `services.portal`, and one of its three values was a
clause rather than a verb phrase, so a member asked for more information
received a comma splice:

    Your Request for assistance — "Help with hospital bill" — reference
    REQ-2026-0001, we need a little more information.

The defaults in `services.notify` are fixed, but templates are seeded into the
database once and are then the church's own to edit — a fresh install would get
the correction and every existing one would keep the broken sentence, since
`install_default_templates` deliberately never overwrites.

So this rewrites exactly one row, and only where it still holds the old default
verbatim. A church that has reworded this email has said what it wants the
message to say, and keeps it.
"""
from django.db import migrations

OLD_BODY = (
    "Dear {member_name},\n\nYour {kind} — \"{subject}\" — reference "
    "{reference}, {phrase}.\n\nSign in to the member portal to read the full "
    "details and to reply if anything further is needed.\n\n{church}")

NEW_BODY = (
    "Dear {member_name},\n\nYour {kind} “{subject}” (reference "
    "{reference}) {phrase}.\n\nSign in to the member portal to read the "
    "full details and to reply if anything further is needed.\n\n{church}")


def _swap(apps, old, new):
    Template = apps.get_model("benevolent", "NotificationTemplate")
    Template.objects.filter(
        event="PORTAL_REQUEST_UPDATED", channel="EMAIL", body=old,
    ).update(body=new)


def forwards(apps, schema_editor):
    _swap(apps, OLD_BODY, NEW_BODY)


def backwards(apps, schema_editor):
    _swap(apps, NEW_BODY, OLD_BODY)


class Migration(migrations.Migration):

    dependencies = [
        ("benevolent", "0033_household_mode_default_and_backfill"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
