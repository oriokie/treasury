"""Move the Camp Meeting Offering goal (a church-wide Trust-fund target,
previously stored on the paired CAMP_EXPENSE Department's offering_fund /
offering_goal fields) into SiteConfig, then clear it off the fund — the
Camp Meeting EXPENSE goal (year_goal) and every other fund/dev-group goal
stay exactly where they were."""
from django.db import migrations


def forwards(apps, schema_editor):
    Department = apps.get_model("departments", "Department")
    SiteConfig = apps.get_model("core", "SiteConfig")
    cfg = SiteConfig.objects.first()
    if not cfg:
        return
    camp = Department.objects.filter(goal_type="CAMP_EXPENSE").exclude(
        offering_goal__isnull=True).first()
    if not camp or not camp.offering_goal:
        return
    if cfg.camp_offering_fund_id is None:
        cfg.camp_offering_fund_id = camp.offering_fund_id
    if cfg.camp_offering_goal is None:
        cfg.camp_offering_goal = camp.offering_goal
    cfg.save()
    # remove it from the fund now that settings owns it (expense goal untouched)
    camp.offering_fund = None
    camp.offering_goal = None
    camp.save(update_fields=["offering_fund", "offering_goal"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0044_historicalsiteconfig_camp_offering_fund_and_more"),
        ("departments", "0001_initial"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
