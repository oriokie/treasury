"""Assign a permanent JV-YYYY-NNNNNN number to every journal entry that
predates journal numbering, in chronological order (date, then id) so the
sequence reads naturally against the general ledger's own ordering. Numbers
are assigned per calendar year of the entry's own accounting date."""
from django.db import migrations


def forwards(apps, schema_editor):
    JournalEntry = apps.get_model("ledger", "JournalEntry")
    JournalSequence = apps.get_model("ledger", "JournalSequence")
    qs = JournalEntry.objects.filter(number__isnull=True).order_by("date", "id")
    counters = {}
    for je in qs.iterator():
        year = je.date.year
        if year not in counters:
            seq, _ = JournalSequence.objects.get_or_create(year=year)
            counters[year] = seq.last_number
        counters[year] += 1
        je.number = f"JV-{year}-{counters[year]:06d}"
        je.save(update_fields=["number"])
    for year, n in counters.items():
        JournalSequence.objects.filter(year=year).update(last_number=n)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0004_journalsequence_journalentry_number_and_more"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
