"""Phase 5 — remove the retired bereaved_exempt_own_levy boolean, now that
0009 has translated every value into bereaved_contribution_policy."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("benevolent", "0009_migrate_bereaved_policy"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="historicalschemepolicy",
            name="bereaved_exempt_own_levy",
        ),
        migrations.RemoveField(
            model_name="schemepolicy",
            name="bereaved_exempt_own_levy",
        ),
    ]
