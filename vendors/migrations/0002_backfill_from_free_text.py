"""Turn the vendor names already written on payables into supplier records.

The register is useless empty. Every church running this already has years of
payables with a name typed into `Payable.vendor`, and asking a treasurer to
re-key them is asking them not to use the feature.

So this creates one supplier per distinct normalised name and points the
payables at it. The normalisation is deliberately the same as
`vendors.models.name_key`: "Mwangi Hardware Ltd", "MWANGI HARDWARE" and
"Mwangi  Hardware" become one supplier, not three.

**The free text is left exactly as it was.** `Payable.vendor` still says what
the invoice said. This only adds the link — so if the grouping gets something
wrong, nothing has been lost and the merge tool can fix it.
"""
import re

from django.db import migrations


def _name_key(raw):
    # duplicated from vendors.models on purpose: a migration must not import
    # application code, because that code will change and this file must keep
    # meaning what it meant on the day it ran
    if not raw:
        return ""
    text = re.sub(r"[^A-Z0-9 ]", " ", str(raw).upper())
    noise = {"LTD", "LIMITED", "CO", "COMPANY", "ENTERPRISES", "ENTERPRISE",
             "SUPPLIERS", "SUPPLIER", "SERVICES", "SERVICE", "AND", "THE",
             "INC", "PLC", "LLC", "GROUP", "TRADERS", "STORES", "GENERAL"}
    words = [w for w in text.split() if w and w not in noise]
    return " ".join(sorted(words))[:160]


def backfill(apps, schema_editor):
    Payable = apps.get_model("cashbook", "Payable")
    Vendor = apps.get_model("vendors", "Vendor")

    # longest spelling wins as the display name — "Mwangi Hardware Ltd" reads
    # better on a supplier record than "Mwangi"
    best = {}
    for payable in Payable.objects.exclude(vendor="").iterator():
        key = _name_key(payable.vendor)
        if not key:
            continue
        name = " ".join((payable.vendor or "").split())
        if key not in best or len(name) > len(best[key]):
            best[key] = name

    created = {}
    for key, name in best.items():
        vendor = Vendor.objects.filter(name_key=key).first()
        if vendor is None:
            vendor = Vendor.objects.create(name=name, name_key=key,
                                           status="ACTIVE")
        created[key] = vendor.pk

    for payable in Payable.objects.exclude(vendor="").iterator():
        key = _name_key(payable.vendor)
        if key in created and payable.supplier_id is None:
            Payable.objects.filter(pk=payable.pk).update(supplier=created[key])


def unlink(apps, schema_editor):
    """Reverse: drop the links. The suppliers themselves are left, since a
    treasurer may have edited them by now and losing that would be worse than
    leaving a register behind."""
    Payable = apps.get_model("cashbook", "Payable")
    Payable.objects.update(supplier=None)


class Migration(migrations.Migration):

    dependencies = [
        ("vendors", "0001_initial"),
        ("cashbook", "0041_expense_vendor_historicalexpense_vendor_and_more"),
    ]

    operations = [migrations.RunPython(backfill, unlink)]
