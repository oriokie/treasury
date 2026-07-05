from django.db import migrations


def create_elder_default(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    from core import rights as R
    Profile.objects.get_or_create(
        name="Elder (default)",
        defaults={"rights": sorted(R.GROUP_RIGHTS[R.roles.ELDER]),
                  "description": "The elder dashboard and executive overview — mirrors the Elder role.",
                  "is_system": True})


def remove_elder_default(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    Profile.objects.filter(name="Elder (default)", is_system=True).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_default_profiles")]
    operations = [migrations.RunPython(create_elder_default, remove_elder_default)]
