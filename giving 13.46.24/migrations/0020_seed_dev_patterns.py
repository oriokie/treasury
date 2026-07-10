from django.db import migrations


def seed(apps, schema_editor):
    DevGroupPattern = apps.get_model("giving", "DevGroupPattern")
    if DevGroupPattern.objects.exists():
        return
    DevGroupPattern.objects.create(
        label="dev / grp / gp + number",
        pattern=r"(?:dev(?:e?l?o?p?)?(?:gr(?:ou)?p?|gp|g)?|gr(?:ou)?p|gp)0*(\d+)",
        kind="NUMBERED", enabled=True, sort_order=10,
        note="Seeded default — matches DEVGR7, devg14, dev grp 5, DEV GP39, etc.")
    DevGroupPattern.objects.create(
        label="development marker (no number)",
        pattern=r"(?:dev(?:elop)?|grp|group|gp)",
        kind="WORD", enabled=True, sort_order=20,
        note="Seeded default — a reference mentioning development with no usable "
             "number is still booked as a development gift, awaiting a group.")


def unseed(apps, schema_editor):
    DevGroupPattern = apps.get_model("giving", "DevGroupPattern")
    DevGroupPattern.objects.filter(note__startswith="Seeded default").delete()


class Migration(migrations.Migration):
    dependencies = [("giving", "0019_devgrouppattern_historicaldevgrouppattern")]
    operations = [migrations.RunPython(seed, unseed)]
