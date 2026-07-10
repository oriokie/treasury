from django.db import migrations


def port(apps, schema_editor):
    RemittanceBatch = apps.get_model("cashbook", "RemittanceBatch")
    PaymentInstrument = apps.get_model("cashbook", "PaymentInstrument")
    for b in RemittanceBatch.objects.filter(payment__isnull=True).exclude(cheque_no=""):
        # a batch already remitted by cheque -> create its settlement instrument.
        # If a matching instrument already exists (from the earlier sync), reuse it.
        inst = (PaymentInstrument.objects
                .filter(instrument_number=b.cheque_no, remittance_batch_id=b.id)
                .first())
        if inst is None:
            inst = PaymentInstrument.objects.create(
                method="CHEQUE",
                instrument_number=b.cheque_no[:40],
                payee="Conference remittance",
                amount=b.total_amount,
                date_issued=b.cheque_date,
                status=("OUTSTANDING" if b.status == "REMITTED" else "DRAFT"),
                source_kind="REMITTANCE",
                remittance_batch_id=b.id,
                recorded_by_id=b.created_by_id,
            )
        b.payment_id = inst.id
        b.save(update_fields=["payment"])


def unport(apps, schema_editor):
    RemittanceBatch = apps.get_model("cashbook", "RemittanceBatch")
    RemittanceBatch.objects.update(payment=None)


class Migration(migrations.Migration):
    dependencies = [
        ("cashbook", "0027_historicalremittancebatch_payment_and_more"),
    ]
    operations = [migrations.RunPython(port, unport)]
