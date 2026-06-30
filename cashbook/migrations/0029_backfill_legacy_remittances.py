from django.db import migrations


def backfill(apps, schema_editor):
    """Legacy 'Remit trust funds' raised standalone remittance expenses with a
    cheque number in voucher_no but no batch or payment instrument. Group those
    orphan remittance expenses by their cheque number and create one settlement
    PaymentInstrument per cheque, linking the expenses to a batch, so all
    historical remittances share the single payment architecture."""
    Expense = apps.get_model("cashbook", "Expense")
    RemittanceBatch = apps.get_model("cashbook", "RemittanceBatch")
    PaymentInstrument = apps.get_model("cashbook", "PaymentInstrument")

    # remittance expenses not already attached to a batch
    orphans = (Expense.objects.filter(category="REMITTANCE", remittance_batch__isnull=True)
               .exclude(voucher_no=""))
    groups = {}
    for ex in orphans:
        groups.setdefault(ex.voucher_no, []).append(ex)

    import datetime as dt
    year = dt.date.today().year
    for cheque, exps in groups.items():
        # skip if an instrument with this number already exists
        if PaymentInstrument.objects.filter(instrument_number=cheque).exists():
            continue
        total = sum((e.amount for e in exps), 0)
        paid = min((e.paid_date or e.date) for e in exps)
        recorder = exps[0].recorded_by_id
        # allocate a batch number
        prefix = f"RB-{year}-"
        last = (RemittanceBatch.objects.filter(batch_number__startswith=prefix)
                .order_by("-batch_number").first())
        seq = (int(last.batch_number.split("-")[-1]) + 1) if last else 1
        batch = RemittanceBatch.objects.create(
            batch_number=f"{prefix}{seq:04d}", total_amount=total,
            status="REMITTED", cheque_no=cheque[:30], cheque_date=paid,
            created_by_id=recorder, approved_by_id=recorder)
        inst = PaymentInstrument.objects.create(
            method="CHEQUE", instrument_number=cheque[:40],
            payee="Conference remittance", amount=total, date_issued=paid,
            status="OUTSTANDING", source_kind="REMITTANCE",
            remittance_batch_id=batch.id, recorded_by_id=recorder)
        batch.payment_id = inst.id
        batch.save(update_fields=["payment"])
        for ex in exps:
            ex.remittance_batch_id = batch.id
            ex.save(update_fields=["remittance_batch"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("cashbook", "0028_port_remittance_cheques")]
    operations = [migrations.RunPython(backfill, noop)]
