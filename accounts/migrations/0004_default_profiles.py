from django.db import migrations


def create_defaults(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    # import the catalogue lazily to avoid app-loading issues
    from core import rights as R
    defaults = {
        "Treasurer (default)": (R.GROUP_RIGHTS[R.roles.TREASURER], "Full access — mirrors the Treasurer role."),
        "Assistant (default)": (R.GROUP_RIGHTS[R.roles.ASSISTANT], "Data entry and reports — mirrors the Assistant role."),
        "Auditor (default)":   (R.GROUP_RIGHTS[R.roles.AUDITOR], "Read-only reports and audit — mirrors the Auditor role."),
        "Leader (default)":    (R.GROUP_RIGHTS[R.roles.LEADER], "Scoped read access — mirrors the Leader role."),
    }
    for name, (rights, desc) in defaults.items():
        Profile.objects.get_or_create(
            name=name,
            defaults={"rights": sorted(rights), "description": desc, "is_system": True})


def remove_defaults(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    Profile.objects.filter(is_system=True).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_profile")]
    operations = [migrations.RunPython(create_defaults, remove_defaults)]
