"""Phase 7 — remove the retired, never-functional notify_member_on_* fields,
now that 0014 has carried forward any church's existing configuration."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("benevolent", "0014_migrate_old_member_notify_flags"),
    ]

    operations = [
        migrations.RemoveField(model_name="historicalbenevolentsettings",
                               name="notify_member_on_arrears"),
        migrations.RemoveField(model_name="historicalbenevolentsettings",
                               name="notify_member_on_benefit_paid"),
        migrations.RemoveField(model_name="historicalbenevolentsettings",
                               name="notify_member_on_enrolment"),
        migrations.RemoveField(model_name="benevolentsettings",
                               name="notify_member_on_arrears"),
        migrations.RemoveField(model_name="benevolentsettings",
                               name="notify_member_on_benefit_paid"),
        migrations.RemoveField(model_name="benevolentsettings",
                               name="notify_member_on_enrolment"),
    ]
