"""Backfill the new split_of link for historical splits, so they benefit
from the same reliable sibling-detection as every split created from now
on — parsed from the "[Split of #N]" tag split_into() has always written
into raw_narration for every child row it creates, which makes this a
completely unambiguous backfill, not a guess."""
import re

from django.db import migrations

_PATTERN = re.compile(r"^\[Split of #(\d+)\]")


def forwards(apps, schema_editor):
    Transaction = apps.get_model("giving", "Transaction")
    candidates = Transaction.objects.filter(
        split_of__isnull=True, raw_narration__startswith="[Split of #")
    for t in candidates.iterator():
        m = _PATTERN.match(t.raw_narration or "")
        if not m:
            continue
        parent_id = int(m.group(1))
        if parent_id == t.pk:
            continue   # a row can never be its own parent
        if not Transaction.objects.filter(pk=parent_id).exists():
            continue   # the parent no longer exists (deleted) — leave unlinked
        t.split_of_id = parent_id
        t.save(update_fields=["split_of_id"])


def backwards(apps, schema_editor):
    pass   # the field itself is removed by reversing 0023, nothing to undo here


class Migration(migrations.Migration):

    dependencies = [
        ("giving", "0023_split_of_link"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
