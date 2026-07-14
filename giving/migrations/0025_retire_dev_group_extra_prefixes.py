"""Retire SiteConfig.dev_group_extra_prefixes into real DevGroupPattern rows.

The field let a church add extra dev-group prefixes as a comma-separated list
("project, phase"), which `giving.services.allocation` turned into the regex
`(?:project|phase)0*(\\d+)`.

That is *precisely* what a DevGroupPattern of kind NUMBERED already does — and
does better: it has a label, a note, an enable/disable switch, an explicit
ordering, and an audit history, and it lives on a page built for exactly this
job. DevGroupPattern's own docstring says it "replaces the previously
hard-coded regexes so treasurers can manage the spellings without a code
change"; the prefix field was the last survivor of the thing it replaced.

Two places to configure one behaviour is how a treasurer adds "project" in
Settings, someone else adds a `project` pattern on the patterns page, and
neither can see the other. So the field goes — but not by silently discarding
what a church has configured. Anything set here becomes a real pattern, visible
and editable on the page where such things belong.
"""
import re

from django.db import migrations


def forwards(apps, schema_editor):
    SiteConfig = apps.get_model("core", "SiteConfig")
    DevGroupPattern = apps.get_model("giving", "DevGroupPattern")

    cfg = SiteConfig.objects.first()
    if cfg is None:
        return
    raw = (getattr(cfg, "dev_group_extra_prefixes", "") or "").strip()
    if not raw:
        return

    order = 500   # after the built-ins, matching the old code's precedence
    for part in raw.split(","):
        p = re.sub(r"[^a-z0-9]", "", part.strip().lower())
        if not p:
            continue
        pattern = r"(?:%s)0*(\d+)" % re.escape(p)
        if DevGroupPattern.objects.filter(pattern=pattern).exists():
            continue
        DevGroupPattern.objects.create(
            label=f"{p} + number",
            pattern=pattern,
            kind="NUMBERED",
            enabled=True,
            sort_order=order,
            note="Migrated from the old 'extra dev-group prefixes' setting, which "
                 "did exactly this but could not be labelled, ordered, disabled or "
                 "audited. Edit or disable it here.")
        order += 1


def backwards(apps, schema_editor):
    pass   # the field is gone by the time this would run


class Migration(migrations.Migration):

    dependencies = [
        ("giving", "0024_backfill_split_of"),
        ("core", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
