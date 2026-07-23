"""Split from reports/views.py (P1-2). Behaviour identical; the
package __init__ reproduces the original module namespace."""
from decimal import Decimal
from django.contrib import messages
from django.db.models import Sum, Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView
from core.permissions import (ReportAccessMixin, TreasurerRequiredMixin,
                              RightRequiredMixin, ReportAccessMixin)
from cashbook.models import Expense
from giving.models import Transaction
from members.models import Member
from ..services import balances
import datetime as _dt
from core.models import SiteConfig
from ..services import budget as budget_svc
from ..services.goals import (sentence_fund_name as _sfund,      # noqa: E402
                             camp_goal_records as _camp_goal_records)
from ._shared import PeriodMixin
from .summaries import _export


class BoardReportSettingsView(TreasurerRequiredMixin, View):
    """Configure which board-report sections appear, their order, and the notes
    shown on the report."""
    template_name = "reports/board_settings.html"

    def get(self, request):
        cfg = SiteConfig.get()
        return render(request, self.template_name, {
            "sections": cfg.board_settings()["sections"],
            "notes": cfg.board_settings()["notes"]})

    def post(self, request):
        cfg = SiteConfig.get()
        # ordering arrives as a list of keys; visibility as checkboxes
        order = request.POST.getlist("order")
        valid = dict(SiteConfig.BOARD_SECTIONS)
        sections = []
        for key in order:
            if key in valid:
                sections.append({"key": key,
                                 "visible": bool(request.POST.get(f"visible_{key}"))})
        cfg.board_config = {"sections": sections,
                            "notes": (request.POST.get("notes") or "")[:4000]}
        cfg.save(update_fields=["board_config"])
        messages.success(request, "Board report settings saved.")
        return redirect("board_settings")

class BoardReportView(PeriodMixin, TemplateView):
    """One-page board summary: position, fund groups, trust, budget, KPIs, plus an
    AI-written narrative (insights, trends, recommendations) with a rule-based
    fallback when the assistant is off or unavailable."""
    template_name = "reports/board_report.html"

    def get_context_data(self, **kwargs):
        from core.services.assistant import board_report_narrative
        from cashbook.views import open_payables_total, open_accruals_total
        from django.db.models.functions import ExtractYear
        ctx = super().get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        cfg = SiteConfig.get()
        paid = [Expense.Status.APPROVED, Expense.Status.PAID]
        rows = balances.department_summary(s, e)
        ctx["totals"] = balances.totals(rows)
        ctx["trust_rows"] = [r for r in rows if r["is_trust"]]
        ctx["local_rows"] = [r for r in rows if not r["is_trust"]]
        ctx["trust_summary"] = balances.trust_summary(s, e)
        ctx["trust_outstanding"] = sum((r["to_remit"] for r in ctx["trust_summary"]), Decimal(0))
        ctx["trust_unreceipted"] = sum((r["unreceipted"] for r in ctx["trust_summary"]), Decimal(0))
        ctx["by_channel"] = balances.income_by_channel(s, e)
        ctx["church"] = cfg.church_name

        # ---- Income & Expenditure statement ----
        income = (Transaction.objects.confirmed_credits().filter(
            date__gte=s, date__lte=e, department__is_trust=False,
            excluded_from_income=False).aggregate(t=Sum("amount"))["t"] or Decimal(0))
        expense = (Expense.objects.filter(date__gte=s, date__lte=e, status__in=paid)
                   .exclude(doc_class=Expense.DocClass.LIABILITY)
                   .exclude(expenditure_type=Expense.ExpenditureType.CAPITAL)
                   .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        capital = (Expense.objects.filter(date__gte=s, date__lte=e, status__in=paid,
                   expenditure_type=Expense.ExpenditureType.CAPITAL)
                   .exclude(doc_class=Expense.DocClass.LIABILITY)
                   .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        ie_cats = (Expense.objects.filter(date__gte=s, date__lte=e, status__in=paid)
                   .exclude(doc_class=Expense.DocClass.LIABILITY)
                   .exclude(expenditure_type=Expense.ExpenditureType.CAPITAL)
                   .values("category").annotate(t=Sum("amount")).order_by("-t"))
        cat_label = dict(Expense.Category.choices)
        ctx["ie_income"] = income
        ctx["ie_expense"] = expense
        ctx["ie_surplus"] = income - expense
        ctx["ie_capital"] = capital
        # Depreciation, donations in kind and disposal gains/losses move net
        # assets without any money moving, so the cash surplus above does not
        # explain the whole change. Shown separately (one shared definition).
        from core.metrics import metrics as _metrics
        _nc = _metrics.non_cash_items(s, e)
        ctx["ie_non_cash"] = _nc
        ctx["ie_depreciation_charge"] = -_nc["depreciation"]   # a deduction, for display
        ctx["ie_expense_accrual"] = expense + _nc["depreciation"]
        ctx["ie_surplus_accrual"] = income - expense + _nc["net"]
        ctx["ie_expense_by_cat"] = [{"label": cat_label.get(r["category"], r["category"]),
                                     "total": r["t"]} for r in ie_cats]

        # ---- Statement of financial position (period end) ----
        income_all = (Transaction.objects.confirmed_credits()
                      .filter(excluded_from_income=False, date__lte=e)
                      .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        payments_all = (Expense.objects.filter(status__in=paid, date__lte=e)
                        .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        opening = (cfg.opening_bank_balance + cfg.opening_cash_on_hand
                   - cfg.opening_unremitted_trust)
        cash_bank = opening + income_all - payments_all
        payables = open_payables_total()
        accruals = open_accruals_total()
        asset_nbv = Decimal(0)
        try:
            from assets.models import FixedAsset
            asset_nbv = sum((a.net_book_value(e) for a in FixedAsset.objects.filter(disposed=False)),
                            Decimal(0))
        except Exception:
            from core.utils import log_exception as _lx; _lx('reports/views.py')
            asset_nbv = Decimal(0)
        trust_liab = ctx["trust_outstanding"]
        total_assets = cash_bank + asset_nbv
        total_liab = trust_liab + payables + accruals
        ctx["sofp"] = {
            "cash_bank": cash_bank, "asset_nbv": asset_nbv, "total_assets": total_assets,
            "trust_liab": trust_liab, "payables": payables, "accruals": accruals,
            "total_liab": total_liab, "net_assets": total_assets - total_liab,
        }

        # ---- Multi-year trend (like-for-like, year-to-date) ----
        # Compare each year only up to the SAME point in the year as the current
        # report period reaches, so a partial current year isn't unfairly shown
        # against full prior years. We cap each year's figures at the month/day
        # the current period ends on.
        cutoff_month, cutoff_day = e.month, e.day
        ytd_label = f" (Jan–{e:%b})" if (cutoff_month, cutoff_day) != (12, 31) else ""

        def _ytd_filter(qs):
            # keep rows whose (month, day) falls on/before the current cutoff
            from django.db.models.functions import ExtractMonth, ExtractDay
            return (qs.annotate(_m=ExtractMonth("date"), _dd=ExtractDay("date"))
                    .filter(Q(_m__lt=cutoff_month)
                            | Q(_m=cutoff_month, _dd__lte=cutoff_day)))

        inc_y = {r["yr"]: r["total"] for r in (_ytd_filter(
                 Transaction.objects.confirmed_credits()
                 .filter(excluded_from_income=False))
                 .annotate(yr=ExtractYear("date"))
                 .values("yr").annotate(total=Sum("amount")))}
        exp_y = {r["yr"]: r["total"] for r in (_ytd_filter(
                 Expense.objects.filter(status__in=paid)
                 .exclude(doc_class=Expense.DocClass.LIABILITY))
                 .annotate(yr=ExtractYear("date")).values("yr")
                 .annotate(total=Sum("amount")))}
        from core.models import HistoricalYear
        hist = {h.year: h for h in HistoricalYear.objects.all()}
        years = sorted(set(inc_y) | set(exp_y) | set(hist))
        ctx["trend_ytd_label"] = ytd_label
        trend = []
        for y in years:
            coll = inc_y.get(y) or (hist[y].collection if y in hist else 0)
            ex = exp_y.get(y) or (hist[y].expenditure if y in hist else 0)
            trend.append({"year": y, "income": coll, "expense": ex,
                          "net": (coll or 0) - (ex or 0)})
        ctx["trend"] = trend

        # ---- narrative (AI with deterministic fallback) ----
        label = f"{s:%d %b %Y} – {e:%d %b %Y}"
        ctx["board_income"] = income
        ctx["board_expenditure"] = expense
        ctx["board_surplus"] = income - expense
        from core.metrics import metrics as _m2
        _bnc = _m2.non_cash_items(s, e)
        ctx["board_non_cash"] = _bnc
        ctx["board_surplus_accrual"] = income - expense + _bnc["net"]
        context_str = self._context_str(ctx, label, income, expense)
        narrative, source, err = None, "fallback", None
        if cfg.llm_enabled and self.request.GET.get("ai") != "0":
            narrative, err = board_report_narrative(context_str, label, cfg)
            if narrative:
                source = "ai"
        if not narrative:
            narrative = self._fallback(ctx, label, income, expense)
        ctx["narrative"] = narrative
        ctx["narrative_source"] = source
        ctx["ai_enabled"] = cfg.llm_enabled
        ctx["ai_error"] = err

        # ---- Goals & targets (#3): expense, offering, group contribution ----
        from departments.models import Department as _Dept
        gyear = e.year

        def _fund_ids(d):
            ids = [d.id]
            for sub in d.subgroups.all():
                ids.extend(_fund_ids(sub))
            return ids

        def _collected(fund):
            return (Transaction.objects.confirmed_credits().filter(
                department_id__in=_fund_ids(fund), excluded_from_income=False,
                date__year=gyear).aggregate(t=Sum("amount"))["t"] or Decimal(0))

        def _goal_row(name, kind, goal, collected):
            goal = goal or Decimal(0)
            return {"name": name, "kind": kind, "goal": goal, "collected": collected,
                    "variance": collected - goal,
                    "pct": int(min(collected / goal * 100, 999)) if goal else 0,
                    "short": max(goal - collected, Decimal(0))}

        goals = []
        # church-wide Camp Meeting goals come from Settings → Goals, not from
        # any individual fund, so fund rows below stay purely per-fund
        goals.extend(_camp_goal_records(gyear))
        for d in _Dept.objects.filter(active=True).prefetch_related("subgroups"):
            if d.goal_type == "CAMP_EXPENSE":
                continue  # migrated to SiteConfig; avoid double rows
            if d.year_goal:
                goals.append(_goal_row(f"{d.name} — annual goal", "Expense (local)",
                                       d.year_goal, _collected(d)))
            if d.offering_goal and d.offering_fund:
                goals.append(_goal_row(f"{d.offering_fund.name} — offering goal",
                                       "Offering (trust)",
                                       d.offering_goal, _collected(d.offering_fund)))
            if d.contribution_goal:
                grp_total = sum((_collected(s) for s in d.subgroups.all()), Decimal(0))
                goals.append(_goal_row(f"{d.name} — group contribution goal",
                                       "Contribution", d.contribution_goal, grp_total))
        ctx["goals"] = goals
        ctx["goals_year"] = gyear

        ctx["board_sections"] = cfg.board_settings()["sections"]
        ctx["board_notes"] = cfg.board_settings()["notes"]
        ctx["bvis"] = {s["key"]: s["visible"] for s in ctx["board_sections"]}
        ctx["border_order"] = [s["key"] for s in ctx["board_sections"] if s["visible"]]
        return ctx

    def _context_str(self, ctx, label, income, expenditure):
        def f(v):
            return f"{float(v or 0):,.0f}"
        top = sorted(ctx["local_rows"], key=lambda r: r["receipts"], reverse=True)[:6]
        lines = [
            f"Period: {label}",
            f"Local income (I&E): {f(income)}",
            f"Local expenditure (I&E): {f(expenditure)}",
            f"Net surplus/(deficit): {f(income - expenditure)}",
            f"Cash & bank at period end: {f(ctx['sofp']['cash_bank'])}",
            f"Net assets: {f(ctx['sofp']['net_assets'])}",
            f"Trust outstanding to remit: {f(ctx['trust_outstanding'])}",
            "Top funds by receipts: " + "; ".join(
                f"{_sfund(r['department'].name)} {f(r['receipts'])}" for r in top if r["receipts"]),
        ]
        if len(ctx["trend"]) > 1:
            lines.append("Year trend (income): " + "; ".join(
                f"{t['year']}: {f(t['income'])}" for t in ctx["trend"][-4:]))
        deficits = [r for r in ctx["local_rows"] if r["closing"] < 0]
        if deficits:
            lines.append("Funds in deficit: " + "; ".join(
                f"{_sfund(r['department'].name)} {f(r['closing'])}" for r in deficits[:6]))
        return "\n".join(lines)

    def _fallback(self, ctx, label, income, expenditure):
        def f(v):
            return f"{float(v or 0):,.0f}"
        surplus = income - expenditure
        top = [r for r in sorted(ctx["local_rows"], key=lambda r: r["receipts"],
                                 reverse=True) if r["receipts"]][:3]
        deficits = [r for r in ctx["local_rows"] if r["closing"] < 0]
        p = ["Executive summary:",
             f"For {label}, collections were {f(income)} against expenditure of "
             f"{f(expenditure)}, a {'surplus' if surplus >= 0 else 'deficit'} of "
             f"{f(abs(surplus))}.",
             "\nKey insights:"]
        if top:
            p.append("- Largest funds: " + ", ".join(
                f"{_sfund(r['department'].name)} ({f(r['receipts'])})" for r in top) + ".")
        p.append(f"- Trust outstanding to remit: {f(ctx['trust_outstanding'])}.")
        if deficits:
            p.append("- Funds in deficit: " + ", ".join(
                r["department"].name for r in deficits[:5]) + ".")
        p.append("\nRecommendations:")
        if ctx["trust_outstanding"] > 0:
            p.append(f"- Remit the outstanding trust of {f(ctx['trust_outstanding'])}.")
        if deficits:
            p.append("- Review and rebalance funds carrying a negative balance.")
        p.append("- Confirm the bank reconciliation and file supporting vouchers.")
        return "\n".join(p)

class PastorReportView(PeriodMixin, TemplateView):
    """Pastoral summary: tithe, offerings, giving by group, participation."""
    template_name = "reports/pastor_report.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        ctx["tithe"] = balances.tithe_total(s, e)
        ctx["by_group"] = balances.giving_by_group(s, e)
        ctx["by_channel"] = balances.income_by_channel(s, e)
        rows = balances.department_summary(s, e)
        ctx["total_income"] = balances.totals(rows)
        from members.models import Member
        ctx["active_members"] = Member.objects.filter(active=True).count()
        givers = (Transaction.objects.confirmed_credits()
                  .filter(date__gte=s, date__lte=e, member__isnull=False)
                  .values("member").distinct().count())
        ctx["givers"] = givers
        ctx["church"] = SiteConfig.get().church_name
        return ctx

class ConferenceSubmissionView(PeriodMixin, TemplateView):
    """Conference submission: trust collected / remitted / to-remit + batches."""
    template_name = "reports/conference_submission.html"

    def get(self, request, *args, **kwargs):
        ctx = self.get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        rows = balances.trust_summary(s, e)
        cfg = SiteConfig.get()
        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Trust fund", "Collected", "Remitted",
                      "Outstanding to remit (receipted)", "Unreceipted (pending)",
                      "Total trust liability"]
            data = [[r["department"].name, r["collected"], r["remitted"], r["to_remit"],
                     r["unreceipted"], r["total_liability"]] for r in rows]
            data.append(["TOTAL",
                         sum((r["collected"] for r in rows), Decimal(0)),
                         sum((r["remitted"] for r in rows), Decimal(0)),
                         sum((r["to_remit"] for r in rows), Decimal(0)),
                         sum((r["unreceipted"] for r in rows), Decimal(0)),
                         sum((r["total_liability"] for r in rows), Decimal(0))])
            ex = _export(request, f"conference_{s}_{e}", header, data, "Conference submission")
            if ex:
                return ex
        ctx["rows"] = rows
        ctx["totals"] = {
            "collected": sum((r["collected"] for r in rows), Decimal(0)),
            "remitted": sum((r["remitted"] for r in rows), Decimal(0)),
            "to_remit": sum((r["to_remit"] for r in rows), Decimal(0)),
            "unreceipted": sum((r["unreceipted"] for r in rows), Decimal(0)),
            "total_liability": sum((r["total_liability"] for r in rows), Decimal(0)),
        }
        ctx["field_name"] = cfg.field_name
        ctx["church"] = cfg.church_name
        return self.render_to_response(ctx)

class BudgetBoardReportView(ReportAccessMixin, TemplateView):
    """Board-facing budget summary: per-department budget by source of funds, with
    Local Church Budget exposure (departmental allocations) and prior-year pegging."""
    template_name = "reports/budget_board.html"

    def get(self, request, *args, **kwargs):
        today = _dt.date.today()
        try:
            year = int(request.GET.get("year", today.year))
        except (TypeError, ValueError):
            year = today.year
        data = budget_svc.board_budget(year)
        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Department", "Trust?", "Own funds", "Local Church Budget",
                      "Other funds", "Total budget", f"{year - 1} total"]
            rows = [[r["dept"].name, "Yes" if r["is_trust"] else "No", r["own"],
                     r["lcb"], r["other"], r["total"], r["prior"]] for r in data["rows"]]
            t = data["totals"]
            rows.append(["TOTAL", "", t["own"], t["lcb"], t["other"], t["budget"], t["prior"]])
            return _export(request, f"board_budget_{year}", header, rows,
                           f"Board Budget Summary {year}")
        ctx = {"year": year, "data": data, "totals": data["totals"],
               "years": range(today.year + 1, today.year - 5, -1)}
        return self.render_to_response(ctx)
