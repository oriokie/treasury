from django.db import migrations


# Map the legacy ChequeRegister statuses onto the new instrument lifecycle.
STATUS_MAP = {
    "ISSUED": "OUTSTANDING",     # issued but not cleared = outstanding at bank
    "CLEARED": "CLEARED",
    "BOUNCED": "STOPPED",        # returned/bounced -> stopped
    "CANCELLED": "VOIDED",
}


def port(apps, schema_editor):
    ChequeRegister = apps.get_model("cashbook", "ChequeRegister")
    PaymentInstrument = apps.get_model("cashbook", "PaymentInstrument")
    if PaymentInstrument.objects.exists():
        return
    for c in ChequeRegister.objects.all():
        if c.expense_id:
            kind = "EXPENSE"
        elif c.remittance_batch_id:
            kind = "REMITTANCE"
        else:
            kind = "MANUAL"
        PaymentInstrument.objects.create(
            method="CHEQUE",
            instrument_number=c.cheque_number or "",
            payee=c.payee or "",
            amount=c.amount,
            date_issued=c.date_issued,
            date_cleared=c.date_cleared,
            status=STATUS_MAP.get(c.status, "OUTSTANDING"),
            source_kind=kind,
            expense_id=c.expense_id,
            remittance_batch_id=c.remittance_batch_id,
            note=c.note or "",
            recorded_by_id=c.recorded_by_id,
        )


def unport(apps, schema_editor):
    PaymentInstrument = apps.get_model("cashbook", "PaymentInstrument")
    PaymentInstrument.objects.filter(method="CHEQUE").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("cashbook", "0025_historicalpaymentinstrument_paymentinstrument_and_more"),
    ]
    operations = [migrations.RunPython(port, unport)]
