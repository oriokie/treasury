"""Phase 0 EAM foundation backfill.

Non-breaking: seeds AssetClass 1:1 from the existing Category enum, creates the
default Organization, and backfills every existing FixedAsset so the new EAM
fields are populated exactly consistently with the old ones:

- asset_class     <- matching class seeded from the row's category
- status          <- DISPOSED if the row is disposed, else IN_SERVICE
- in_service_on   <- acquired_on (assets currently depreciate from acquisition;
                     commissioning-date depreciation is a Phase 1 opt-in)
- tag             <- the existing `reference` if it's unique, else FA-000N
- church          <- the default organization

No figures change: category and all depreciation inputs are untouched, so
nbv_total() and the financial statements are identical after this runs.
"""
from django.db import migrations


# (code, name, depreciable, is_cwip) — mirrors FixedAsset.Category semantics
CLASS_SEED = [
    ("LAND", "Land", False, False),
    ("CONSTRUCTION", "Construction in progress", False, True),
    ("BUILDING", "Buildings", True, False),
    ("FURNITURE", "Furniture & fittings", True, False),
    ("EQUIPMENT", "Equipment", True, False),
    ("VEHICLE", "Motor vehicles", True, False),
    ("IT", "Computers & IT", True, False),
    ("MUSICAL", "Musical instruments", True, False),
    ("OTHER", "Other", True, False),
]


def forward(apps, schema_editor):
    AssetClass = apps.get_model("assets", "AssetClass")
    DepreciationRule = apps.get_model("assets", "DepreciationRule")
    FixedAsset = apps.get_model("assets", "FixedAsset")
    Organization = apps.get_model("core", "Organization")
    SiteConfig = apps.get_model("core", "SiteConfig")

    # default organization
    org = Organization.objects.filter(is_default=True).first()
    if org is None:
        cfg = SiteConfig.objects.first()
        name = (getattr(cfg, "church_name", "") or "Our Church") if cfg else "Our Church"
        org = Organization.objects.create(name=name, slug="default", is_default=True)

    # asset classes, carrying any existing per-category depreciation policy
    rules = {r.category: r for r in DepreciationRule.objects.all()}
    by_code = {}
    for code, name, depreciable, is_cwip in CLASS_SEED:
        rule = rules.get(code)
        ac, _ = AssetClass.objects.get_or_create(
            code=code,
            defaults=dict(
                name=name, depreciable=depreciable, is_cwip=is_cwip,
                default_method=(rule.method if rule else "STRAIGHT"),
                default_rate=(rule.rate if rule else 0),
            ),
        )
        by_code[code] = ac

    # backfill every asset
    used_tags = set()
    seq = 0
    for a in FixedAsset.objects.all().order_by("id"):
        a.asset_class = by_code.get(a.category) or by_code["OTHER"]
        a.status = "DISPOSED" if a.disposed else "IN_SERVICE"
        a.in_service_on = a.acquired_on
        a.church_id = org.id
        # tag: prefer the existing reference when unique, else generate
        ref = (a.reference or "").strip()
        if ref and ref not in used_tags:
            a.tag = ref[:40]
        else:
            seq += 1
            tag = f"FA-{seq:04d}"
            while tag in used_tags:
                seq += 1
                tag = f"FA-{seq:04d}"
            a.tag = tag
        used_tags.add(a.tag)
        a.save(update_fields=["asset_class", "status", "in_service_on",
                              "church", "tag"])


def backward(apps, schema_editor):
    # non-destructive: leave seeded rows in place (fields are nullable)
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0005_fixedasset_church_fixedasset_custodian_and_more"),
        ("core", "0054_organization"),
    ]
    operations = [migrations.RunPython(forward, backward)]
