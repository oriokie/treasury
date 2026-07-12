"""Phase 10 — add the correctly-named notify_on_committee_pending field.
notify_committee_on_pending_vote is removed separately, in 0018, once 0017
has carried its value forward — a data migration needs the old column to
still exist to read it."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('benevolent', '0015_remove_dead_member_notify_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='benevolentsettings',
            name='notify_on_committee_pending',
            field=models.BooleanField(default=True, help_text="Tell TREASURY STAFF when a case is first routed to the committee for a decision - a staff awareness notice, like its siblings above. Distinct from notify_committee_vote_needed below, which tells the COMMITTEE MEMBERS THEMSELVES it is their turn to vote."),
        ),
        migrations.AddField(
            model_name='historicalbenevolentsettings',
            name='notify_on_committee_pending',
            field=models.BooleanField(default=True, help_text="Tell TREASURY STAFF when a case is first routed to the committee for a decision - a staff awareness notice, like its siblings above. Distinct from notify_committee_vote_needed below, which tells the COMMITTEE MEMBERS THEMSELVES it is their turn to vote."),
        ),
    ]
