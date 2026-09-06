# Generated manually for DevelopmentGroup.match_code
from django.db import migrations, models


def backfill_codes(apps, schema_editor):
    DevelopmentGroup = apps.get_model("departments", "DevelopmentGroup")
    from core.codes import generate_match_code
    used = set(
        DevelopmentGroup.objects.exclude(match_code__isnull=True)
        .exclude(match_code="")
        .values_list("match_code", flat=True)
    )
    for g in DevelopmentGroup.objects.filter(
            models.Q(match_code__isnull=True) | models.Q(match_code="")):
        code = generate_match_code("DEV")
        for _ in range(40):
            if code not in used:
                break
            code = generate_match_code("DEV")
        used.add(code)
        g.match_code = code
        g.save(update_fields=["match_code"])


class Migration(migrations.Migration):

    dependencies = [
        ("departments", "0021_alter_departmentstatuslog_department"),
    ]

    operations = [
        migrations.AddField(
            model_name="developmentgroup",
            name="match_code",
            field=models.CharField(
                blank=True, db_index=True, help_text="Code in a bank reference "
                "that auto-allocates to this development group (e.g. DEV7K2M).",
                max_length=16, null=True, unique=True),
        ),
        migrations.RunPython(backfill_codes, migrations.RunPython.noop),
    ]
