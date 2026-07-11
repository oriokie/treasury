"""Phase 4 — split the old FEE / DONATION kinds into the real taxonomy.

Phase 2 had four kinds: DUES, LEVY, FEE, DONATION. Phase 4 needs the distinctions
that a contribution engine actually has to make — a registration fee is not a
renewal fee, and a member meeting a voluntary obligation is not a stranger's gift.

The translation is lossless because the old kinds are strictly coarser than the new
ones, and where an old FEE could have been either a registration or a renewal, the
receipt's own reference says which (record_fee wrote "REGISTRATION FEE" or "RENEWAL
FEE" into it). Where even that is silent, it falls back to REGISTRATION, which is
what the overwhelming majority of historical fees were — and the row is left with
its original reference intact, so the guess is checkable rather than hidden.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Contribution = apps.get_model("benevolent", "BenevolentContribution")

    for c in Contribution.objects.select_related("transaction").iterator():
        if c.kind == "FEE":
            ref = (c.transaction.reference or "").upper() if c.transaction_id else ""
            note = (c.note or "").upper()
            c.kind = ("RENEWAL" if ("RENEWAL" in ref or "RENEWAL" in note)
                      else "REGISTRATION")
            c.save(update_fields=["kind"])
        elif c.kind == "DONATION":
            # a donation FROM AN ENROLLED MEMBER was, in the old vocabulary, simply
            # "not dues" — which in the new one is a voluntary contribution. A
            # donation from someone with no membership stays a donation.
            if c.membership_id:
                c.kind = "VOLUNTARY"
                c.save(update_fields=["kind"])


def backwards(apps, schema_editor):
    Contribution = apps.get_model("benevolent", "BenevolentContribution")
    reverse = {"REGISTRATION": "FEE", "RENEWAL": "FEE", "VOLUNTARY": "DONATION",
               "PENALTY": "DONATION"}
    for c in Contribution.objects.iterator():
        if c.kind in reverse:
            c.kind = reverse[c.kind]
            c.save(update_fields=["kind"])


class Migration(migrations.Migration):

    dependencies = [
        ("benevolent", "0006_benevolentcontribution_allocated_automatically_and_more"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
