"""Data migration: split the historical processed_via_envelope flag into two
distinct states.

Until now, processed_via_envelope=True meant either (a) a SYSTEM envelope was
created for the gift, or (b) the gift was marked as handled on paper (the bulk
"mark processed" tool, or the per-gift "mark only" action). We now distinguish:

  * processed_via_envelope = a system envelope record exists.
  * manual_receipt         = receipted on paper, no system envelope.

Rule: a processed item that has NO linked envelope record (neither as an
Envelope.bank_transaction nor via an EnvelopeLine.transaction) was a paper /
mark-only receipt → set manual_receipt=True and clear processed_via_envelope.
Items that DO have an envelope keep processed_via_envelope=True.
"""
from django.db import migrations


def split_flags(apps, schema_editor):
    Transaction = apps.get_model("giving", "Transaction")
    Envelope = apps.get_model("envelopes", "Envelope")
    EnvelopeLine = apps.get_model("envelopes", "EnvelopeLine")

    # ids of transactions that genuinely have a system envelope
    with_env = set(Envelope.objects.exclude(bank_transaction__isnull=True)
                   .values_list("bank_transaction_id", flat=True))
    with_env |= set(EnvelopeLine.objects.exclude(transaction__isnull=True)
                    .values_list("transaction_id", flat=True))

    # processed items with no envelope record → they were paper / mark-only
    to_manual = (Transaction.objects.filter(processed_via_envelope=True)
                 .exclude(id__in=with_env))
    # update in bulk; history rows are left as-is (audit trail of the old state)
    to_manual.update(manual_receipt=True, processed_via_envelope=False)


def reverse(apps, schema_editor):
    # fold manual_receipt back into processed_via_envelope (best-effort reverse)
    Transaction = apps.get_model("giving", "Transaction")
    Transaction.objects.filter(manual_receipt=True).update(
        processed_via_envelope=True, manual_receipt=False)


class Migration(migrations.Migration):
    dependencies = [
        ("giving", "0014_historicaltransaction_manual_receipt_and_more"),
        ("envelopes", "0001_initial"),
    ]
    operations = [migrations.RunPython(split_flags, reverse)]
