from django.db import migrations


def backfill(apps, schema_editor):
    """Set service_sabbath = the Saturday of each existing transaction's week, so
    historical Sabbaths are unchanged by the new field."""
    import datetime as dt
    Transaction = apps.get_model("giving", "Transaction")
    rows = Transaction.objects.filter(service_sabbath__isnull=True).only("id", "date")
    batch = []
    for t in rows.iterator():
        if t.date:
            t.service_sabbath = t.date + dt.timedelta(days=(5 - t.date.weekday()) % 7)
            batch.append(t)
        if len(batch) >= 500:
            Transaction.objects.bulk_update(batch, ["service_sabbath"])
            batch = []
    if batch:
        Transaction.objects.bulk_update(batch, ["service_sabbath"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("giving", "0010_historicaltransaction_service_sabbath_and_more")]
    operations = [migrations.RunPython(backfill, noop)]
