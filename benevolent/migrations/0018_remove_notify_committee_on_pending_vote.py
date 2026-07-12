"""Phase 10 — remove the retired, wrongly-named field, now that 0017 has
carried its value forward to notify_on_committee_pending."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("benevolent", "0017_migrate_committee_pending_flag"),
    ]

    operations = [
        migrations.RemoveField(model_name="historicalbenevolentsettings",
                               name="notify_committee_on_pending_vote"),
        migrations.RemoveField(model_name="benevolentsettings",
                               name="notify_committee_on_pending_vote"),
    ]
