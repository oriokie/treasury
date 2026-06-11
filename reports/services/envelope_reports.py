"""Aggregates for the envelope reports (per-Sabbath statement and monthly
summary), read from EnvelopeLine so they cover both cash and bank envelopes.

Entries are grouped by the *Sabbath they belong to* — the next Saturday on/after
the entry date — so Sunday–Friday giving rolls into the coming Sabbath and
bank-pulled donations (dated on weekdays) are still counted.
"""
import datetime as dt
from collections import defaultdict
from decimal import Decimal

from departments.models import Department
from envelopes.models import Envelope


from core.utils import sabbath_bucket, saturdays_of_month as _saturdays_in_month


def _envelopes_for_bucket(target_saturday):
    """Envelopes whose Sabbath bucket equals target_saturday."""
    lo = target_saturday - dt.timedelta(days=6)
    qs = (Envelope.objects.filter(date__gte=lo, date__lte=target_saturday)
          .select_related("member").prefetch_related("lines__department"))
    return [e for e in qs if sabbath_bucket(e.date) == target_saturday]


def _fund_label(dept):
    """A reporting label: sub-accounts read on their own; never the bare parent."""
    return dept.name


def sabbath_statement(date):
    """Per-contributor listing for one Sabbath. Only funds actually given to
    (non-zero) appear; parent funds are excluded (giving lands on sub-accounts)."""
    envs = sorted(_envelopes_for_bucket(date), key=lambda e: e.receipt_no)

    fund_totals = defaultdict(Decimal)
    fund_obj = {}
    rows = []
    for env in envs:
        cells = {}
        for line in env.lines.all():
            d = line.department
            if d.subgroups.exists():         # skip umbrella/parent funds
                continue
            cells[d.id] = cells.get(d.id, Decimal(0)) + line.amount
            fund_totals[d.id] += line.amount
            fund_obj[d.id] = d
        rows.append({"envelope": env, "cells": cells, "total": env.total})

    # only funds with a non-zero total, trust first then by name
    present = [fund_obj[fid] for fid, t in fund_totals.items() if t]
    funds = sorted(present, key=lambda d: (not d.is_trust, d.name))
    trust_funds = [f for f in funds if f.is_trust]
    local_funds = [f for f in funds if not f.is_trust]
    trust_total = sum((fund_totals[f.id] for f in trust_funds), Decimal(0))
    local_total = sum((fund_totals[f.id] for f in local_funds), Decimal(0))
    return {
        "date": date, "funds": funds, "rows": rows, "fund_totals": dict(fund_totals),
        "trust_funds": trust_funds, "local_funds": local_funds,
        "trust_total": trust_total, "local_total": local_total,
        "grand_total": trust_total + local_total,
    }


def monthly_summary(year, month):
    """OFFERING SUMMARY style: funds (rows) x Sabbaths (cols) + total, trust then
    local. Only funds with giving in the month appear; parents excluded."""
    saturdays = _saturdays_in_month(year, month)
    grid = defaultdict(lambda: defaultdict(Decimal))   # fid -> {saturday: total}
    fund_obj = {}
    for sat in saturdays:
        for env in _envelopes_for_bucket(sat):
            for line in env.lines.all():
                d = line.department
                if d.subgroups.exists():
                    continue
                grid[d.id][sat] += line.amount
                fund_obj[d.id] = d

    present = [fund_obj[fid] for fid in grid if sum(grid[fid].values())]
    funds = sorted(present, key=lambda d: (not d.is_trust, d.name))

    def build(fund_list):
        out = []
        for f in fund_list:
            cols = [grid[f.id].get(s, Decimal(0)) for s in saturdays]
            out.append({"fund": f, "cols": cols, "total": sum(cols, Decimal(0))})
        return out

    trust_rows = build([f for f in funds if f.is_trust])
    local_rows = build([f for f in funds if not f.is_trust])

    def col_total(rows, i):
        return sum((r["cols"][i] for r in rows), Decimal(0))

    return {
        "year": year, "month": month, "saturdays": saturdays,
        "trust_rows": trust_rows, "local_rows": local_rows,
        "trust_col_totals": [col_total(trust_rows, i) for i in range(len(saturdays))],
        "local_col_totals": [col_total(local_rows, i) for i in range(len(saturdays))],
        "trust_total": sum((r["total"] for r in trust_rows), Decimal(0)),
        "local_total": sum((r["total"] for r in local_rows), Decimal(0)),
    }
