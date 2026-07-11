"""Phase 5 — translate the old two-boolean bereaved model into the new,
explicit four-way `bereaved_contribution_policy`.

The mapping is lossless for every combination the old fields could actually
hold (the model's own docstring called the pair "mutually exclusive", and this
preserves that ordering exactly where a church had somehow set both):

    exempt=True,  deduct=*      -> EXEMPT              (exempt always won)
    exempt=False, deduct=True   -> CONTRIBUTES          (deduct flag kept as-is —
                                                          it is now an orthogonal
                                                          "how it's collected"
                                                          modifier, not a
                                                          category of its own)
    exempt=False, deduct=False  -> CONTRIBUTES          (the old fall-through:
                                                          levied normally, on
                                                          the roster)

`bereaved_deduct_own_levy` and `bereaved_dues_waiver_months` are untouched —
only `bereaved_exempt_own_levy` is being retired.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Policy = apps.get_model("benevolent", "SchemePolicy")
    for p in Policy.objects.all().iterator():
        p.bereaved_contribution_policy = (
            "EXEMPT" if p.bereaved_exempt_own_levy else "CONTRIBUTES")
        p.save(update_fields=["bereaved_contribution_policy"])


def backwards(apps, schema_editor):
    Policy = apps.get_model("benevolent", "SchemePolicy")
    for p in Policy.objects.all().iterator():
        p.bereaved_exempt_own_levy = (
            p.bereaved_contribution_policy == "EXEMPT")
        p.save(update_fields=["bereaved_exempt_own_levy"])


class Migration(migrations.Migration):

    dependencies = [
        ("benevolent", "0008_phase5_case_management"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
