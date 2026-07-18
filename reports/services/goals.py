"""Goal / target records for reports — Camp Meeting expense and offering goals.

Pure aggregation logic, extracted verbatim from reports/views.py where it did
not belong (a data query is not a view). Behaviour is unchanged; reports/views.py
re-exports these names so every existing `from reports.views import ...` and
`reports.views._camp_goal_records(...)` call keeps working exactly as before.
"""
from decimal import Decimal

from django.db.models import Sum

from departments.models import Department
from giving.models import Transaction


def sentence_fund_name(name):
    """Sentence-case a fund/department name for narrative text (many are stored
    in ALL CAPS). Same rule as the `sentence_fund` template filter — kept in
    sync so Python-built narrative strings match table cells."""
    from core.templatetags.treasury_extras import sentence_fund
    return sentence_fund(name)


def camp_goal_records(year):
    """Camp Meeting expense goal (Local fund flagged CAMP_EXPENSE, aggregated
    over its sub-accounts) paired with the Camp Meeting Offering goal — a single
    church-wide Trust-fund target configured in Settings → Goals rather than on
    any individual fund."""
    from core.models import SiteConfig

    def _ids(d):
        out = [d.id]
        for sub in d.subgroups.all():
            out.extend(_ids(sub))
        return out

    def _collected(fund):
        if fund is None:
            return Decimal(0)
        return (Transaction.objects.confirmed_credits().filter(
            department_id__in=_ids(fund), excluded_from_income=False,
            date__year=year).aggregate(t=Sum("amount"))["t"] or Decimal(0))

    def _row(name, kind, goal, fund):
        goal = goal or Decimal(0)
        col = _collected(fund)
        return {"name": name, "kind": kind, "goal": goal, "collected": col,
                "variance": col - goal,
                "pct": int(min(col / goal * 100, 999)) if goal else 0,
                "short": max(goal - col, Decimal(0))}

    rows = []
    # deterministic + defensive: if more than one fund is (mis)flagged
    # CAMP_EXPENSE, prefer the one that actually has a goal set rather than
    # an arbitrary DB-order pick (an unordered .first() is not guaranteed
    # stable across databases, and picking one with no year_goal would make
    # the goal silently vanish from every report even though it's really set
    # on a different fund).
    camp = (Department.objects.filter(active=True, goal_type="CAMP_EXPENSE")
            .prefetch_related("subgroups")
            .order_by("-year_goal", "id").first())
    if camp and camp.year_goal:
        rows.append(_row("Camp Meeting Expense Goal", "Expense (local)",
                         camp.year_goal, camp))
    cfg = SiteConfig.get()
    if cfg.camp_offering_goal and cfg.camp_offering_fund_id:
        rows.append(_row("Camp Meeting Offering Goal", "Offering (trust)",
                         cfg.camp_offering_goal, cfg.camp_offering_fund))
    return rows
