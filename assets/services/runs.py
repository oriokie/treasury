"""Depreciation run generation and posting (EAM Phase 1).

`generate_run(year, month)` computes each live asset's charge for the month
(via the monthly engine, so it ties to the register's accumulated depreciation)
and stores a DepreciationRun with one line per asset. `post_run(run)` books it
to the general ledger through the posting engine. Runs in a closed accounting
period are locked and cannot be regenerated or reposted.
"""
import calendar
import datetime as dt
from decimal import Decimal

from django.utils import timezone


def _policy_caches():
    from assets.models import DepreciationRule
    from core.models import SiteConfig
    rules = {r.category: r for r in DepreciationRule.objects.all()}
    return rules, SiteConfig.get()


def generate_run(year, month, user=None):
    """Create (or regenerate) the draft run for a month. Refuses if a posted/
    locked run already exists for that month."""
    from assets.models import DepreciationRun, DepreciationLine, FixedAsset
    from assets.services import depreciation as dep
    existing = DepreciationRun.objects.filter(year=year, month=month).first()
    if existing and existing.status != DepreciationRun.Status.DRAFT:
        raise ValueError(f"A {existing.get_status_display().lower()} run already "
                         f"exists for {year}-{month:02d}.")
    rules, cfg = _policy_caches()
    run_date = dt.date(year, month, calendar.monthrange(year, month)[1])
    if existing:
        existing.lines.all().delete()
        run = existing
        run.run_date = run_date
    else:
        run = DepreciationRun(year=year, month=month, run_date=run_date)
    run.created_by = user
    run.total_charge = Decimal(0)
    run.save()

    lines, total = [], Decimal(0)
    # Include disposed assets too: charge_for_month returns their final charge up
    # to the disposal month and zero thereafter, so accumulated depreciation ties
    # to the disposal posting.
    assets = FixedAsset.objects.select_related("asset_class", "department")
    for a in assets:
        amt = dep.charge_for_month(a, year, month, rules=rules, cfg=cfg)
        if amt and amt > 0:
            acc_after = dep.accumulated_depreciation(a, run_date, rules=rules, cfg=cfg)
            lines.append(DepreciationLine(run=run, asset=a, department=a.department,
                                          amount=amt, accumulated_after=acc_after))
            total += amt
    DepreciationLine.objects.bulk_create(lines)
    run.total_charge = total
    run.save(update_fields=["total_charge"])
    return run


def post_run(run):
    """Post a draft run to the ledger and mark it POSTED."""
    from assets.models import DepreciationRun
    from ledger.services import posting
    if run.status == DepreciationRun.Status.LOCKED:
        raise ValueError("This run is in a closed period and cannot be posted.")
    entry = posting.post_depreciation_run(run)
    run.status = DepreciationRun.Status.POSTED
    run.posted_at = timezone.now()
    run.journal = entry
    run.save(update_fields=["status", "posted_at", "journal"])
    return run
