"""Phase 8 — Reporting, Analytics & Dashboards.

Every report category the brief names — operational dashboards, KPIs,
contribution summaries, household reports, membership reports, committee
reports, case reports, fund balances, arrears analysis, benefit payments,
financial statements, audit reports, analytical dashboards — through the
SAME Generic Report Engine every other report in this system already uses
(``core.reporting``), not a parallel one built for this module alone.

That reuse is not a style preference; it is what makes the rest of the
brief true almost for free. A component here that returns a well-formed
``SectionData`` automatically gets: an HTML page, CSV/XLSX/PDF/DOCX export,
a print view, permission enforcement, a dependency map, and a place in the
report library's search/favourites/usage tracking — none of it written in
this file. "Advanced filtering... printable statements... exports" is the
engine's job; this file's job is only to say what a benevolent report
actually shows, using data this module already has.

No component here computes a new financial figure. Every money value is
either read straight from the Financial Metrics Registry via ``ctx.metric``
(fund balances, contributions, payouts, commitments, arrears) or is a
breakdown of one of those totals (arrears_analysis breaks down the
benevolent_arrears total member by member; committee/household/audit
sections are operational, not financial, and read the same service
functions the module's own screens already use).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from core.reporting.components import ComponentSection, component_registry
from core.reporting.engine import Column, Filter, Report, Row, SectionData, registry


def _can_view_benevolent(user):
    from core import roles
    return roles.can_view_benevolent(user)


def _can_manage_benevolent(user):
    from core import roles
    return roles.can_manage_benevolent(user)


def _n(v):
    return v if v is not None else Decimal(0)


def _scheme_filter_value(filters):
    """The ?scheme= filter, resolved to a BenevolentScheme or None (= every
    scheme). Accepts either the scheme's short CODE (e.g. "BEN" — what a
    treasurer actually sees everywhere else in the module, including on
    every case number) or its numeric id, since the filter renders as a
    plain text box, not a dropdown (see the note in register_reports about
    why a live dropdown was not built here)."""
    raw = (filters.get("scheme") or "").strip()
    if not raw:
        return None
    from benevolent.models import BenevolentScheme
    if raw.isdigit():
        return BenevolentScheme.objects.filter(pk=int(raw)).first()
    return BenevolentScheme.objects.filter(code__iexact=raw).first()


SCHEME_FILTER = Filter("scheme", "Scheme code (e.g. BEN) — blank for all", kind="text")


# ===========================================================================
# 1. Operational dashboard & KPIs
# ===========================================================================

class BenevolentKpiComponent(ComponentSection):
    key = "benevolent_kpis"
    title = "Key figures"
    declared_metrics = ("benevolent_scheme_summary", "benevolent_arrears",
                        "benevolent_commitments")

    def render(self, ctx, filters):
        from benevolent.models import BenevolentCase, SchemeMembership
        scheme = _scheme_filter_value(filters)
        rows = ctx.metric("benevolent_scheme_summary", ctx.start, ctx.end)
        if scheme is not None:
            rows = [r for r in rows if r["scheme"].pk == scheme.pk]

        closing = sum((_n(r["closing"]) for r in rows), Decimal(0))
        members = sum(r["members"] for r in rows)
        open_cases = sum(r["open_cases"] for r in rows)
        committed = sum((_n(r["committed"]) for r in rows), Decimal(0))
        arrears = ctx.metric("benevolent_arrears", scheme, ctx.end)

        cards = [
            ("Active members", members),
            ("Open cases", open_cases),
            ("Combined fund balance", closing),
            ("Committed (approved, unpaid)", committed),
            ("Members' arrears", arrears),
        ]
        columns = [Column("label", "Metric"), Column("value", "Value", numeric=True)]
        return SectionData(key=self.key, title=self.title, columns=columns,
                           rows=[Row(cells={"label": l, "value": v}) for l, v in cards],
                           kind="kpi")


class BenevolentSchemeSummaryComponent(ComponentSection):
    key = "benevolent_scheme_summary_table"
    title = "Scheme summary"
    declared_metrics = ("benevolent_scheme_summary",)

    def render(self, ctx, filters):
        scheme = _scheme_filter_value(filters)
        rows = ctx.metric("benevolent_scheme_summary", ctx.start, ctx.end)
        if scheme is not None:
            rows = [r for r in rows if r["scheme"].pk == scheme.pk]
        columns = [
            Column("scheme", "Scheme"), Column("opening", "Opening", numeric=True),
            Column("contributions", "Contributions", numeric=True),
            Column("payouts", "Benefits paid", numeric=True),
            Column("closing", "Closing", numeric=True, drilldown=True),
            Column("members", "Members", numeric=True),
            Column("open_cases", "Open cases", numeric=True),
            Column("committed", "Committed", numeric=True),
        ]
        out_rows = []
        for r in rows:
            out_rows.append(Row(cells={
                "scheme": r["scheme"].name, "opening": r["opening"],
                "contributions": r["contributions"], "payouts": r["payouts"],
                "closing": r["closing"], "members": r["members"],
                "open_cases": r["open_cases"], "committed": r["committed"]},
                url=f"/benevolent/schemes/{r['scheme'].pk}/"))
        total_row = None
        if out_rows:
            from benevolent.services.reporting import totals
            t = totals(rows)
            total_row = Row(cells={"scheme": "TOTAL", "opening": t["opening"],
                                   "contributions": t["contributions"],
                                   "payouts": t["payouts"], "closing": t["closing"],
                                   "members": t["members"], "open_cases": t["open_cases"],
                                   "committed": t["committed"]}, emphasis=True)
        return SectionData(key=self.key, title=self.title, columns=columns,
                           rows=out_rows, total=total_row,
                           note="Each scheme's financial columns are its fund's own "
                                "figures from the Financial Metrics Registry — the "
                                "same numbers the fund statement shows for it.")


class BenevolentCaseStatsComponent(ComponentSection):
    key = "benevolent_case_stats"
    title = "Cases by status"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.services.reporting import case_statistics
        scheme = _scheme_filter_value(filters)
        stats = case_statistics(ctx.start, ctx.end, scheme)
        columns = [Column("status", "Status"), Column("n", "Cases", numeric=True)]
        rows = [Row(cells={"status": label, "n": stats["by_status"].get(key, 0)})
                for key, label in _case_status_choices()]
        note = (f"{stats['awaiting_assessment']} awaiting assessment, "
               f"{stats['awaiting_approval']} awaiting approval, "
               f"{stats['awaiting_payment']} awaiting payment.")
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           note=note)


def _case_status_choices():
    from benevolent.models import BenevolentCase
    return BenevolentCase.Status.choices


# ===========================================================================
# 2. Contribution summary
# ===========================================================================

class BenevolentContributionSummaryComponent(ComponentSection):
    key = "benevolent_contribution_summary"
    title = "Contributions by kind"
    declared_metrics = ("benevolent_contributions",)

    def render(self, ctx, filters):
        from django.db.models import Count, Sum
        from benevolent.models import BenevolentContribution
        scheme = _scheme_filter_value(filters)
        qs = BenevolentContribution.objects.filter(transaction__is_reversed=False)
        if scheme is not None:
            qs = qs.filter(scheme=scheme)
        if ctx.start:
            qs = qs.filter(transaction__date__gte=ctx.start)
        if ctx.end:
            qs = qs.filter(transaction__date__lte=ctx.end)
        by_kind = (qs.values("kind")
                  .annotate(n=Count("id"), total=Sum("transaction__amount"))
                  .order_by("-total"))
        columns = [Column("kind", "Kind"), Column("n", "Count", numeric=True),
                  Column("total", "Total", numeric=True)]
        rows = [Row(cells={"kind": r["kind"].title(), "n": r["n"],
                          "total": r["total"] or Decimal(0)}) for r in by_kind]
        total_amt = ctx.metric("benevolent_contributions", ctx.start, ctx.end, scheme)
        total_row = Row(cells={"kind": "TOTAL", "n": sum(r["n"] for r in by_kind),
                               "total": total_amt}, emphasis=True) if rows else None
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           total=total_row,
                           note="Totals agree with benevolent_contributions in the "
                                "Financial Metrics Registry — reversed receipts are "
                                "excluded here exactly as they are there.")


# ===========================================================================
# 3. Membership & household reports
# ===========================================================================

class BenevolentMembershipComponent(ComponentSection):
    key = "benevolent_membership"
    title = "Membership register"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.models import SchemeMembership
        scheme = _scheme_filter_value(filters)
        qs = SchemeMembership.objects.select_related("member", "scheme")
        if scheme is not None:
            qs = qs.filter(scheme=scheme)
        qs = qs.order_by("scheme__name", "member__name")
        columns = [Column("member", "Member", drilldown=True),
                  Column("scheme", "Scheme"), Column("number", "Number"),
                  Column("status", "Status"), Column("standing", "Standing"),
                  Column("joined_on", "Joined")]
        rows = [Row(cells={"member": m.member.name, "scheme": m.scheme.code,
                          "number": m.number, "status": m.get_status_display(),
                          "standing": m.get_standing_display(),
                          "joined_on": m.joined_on},
                   url=f"/benevolent/members/{m.pk}/")
               for m in qs[:2000]]     # a hard cap protects a very large register;
                                       # the CSV/XLSX export is the tool for "all of it"
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           note=(f"{qs.count()} membership(s)"
                                + (" (showing the first 2,000 — export for the rest)"
                                   if qs.count() > 2000 else ".")))


class BenevolentHouseholdComponent(ComponentSection):
    key = "benevolent_households"
    title = "Households"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.services.reporting import household_report
        scheme = _scheme_filter_value(filters)
        rows_data = household_report(scheme)
        columns = [Column("household", "Household", drilldown=True),
                  Column("scheme", "Scheme"), Column("size", "Size", numeric=True),
                  Column("dependants", "Dependants")]
        rows = [Row(cells={
            "household": r["household_name"], "scheme": r["scheme"].code,
            "size": r["size"],
            "dependants": ", ".join(d.display_name for d in r["dependants"]) or "—"},
            url=f"/benevolent/members/{r['membership'].pk}/") for r in rows_data]
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           note=f"{len(rows)} household registration(s).")


# ===========================================================================
# 4. Committee report
# ===========================================================================

class BenevolentCommitteeComponent(ComponentSection):
    key = "benevolent_committee"
    title = "Committee roster & activity"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.services.reporting import committee_report
        scheme = _scheme_filter_value(filters)
        rows_data = committee_report(scheme)
        columns = [Column("scheme", "Scheme"), Column("person", "Person"),
                  Column("role", "Role"), Column("seated_since", "Seated since"),
                  Column("votes_cast", "Decisions recorded", numeric=True)]
        rows = [Row(cells={
            "scheme": r["scheme"].code,
            "person": r["user"].get_full_name() or r["user"].username,
            "role": r["role"], "seated_since": r["seated_since"].date(),
            "votes_cast": r["votes_cast"]}) for r in rows_data]
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           note="A scheme with no roster configured shows no rows here "
                                "— it still accepts votes from anyone holding the "
                                "general committee right (see Committee Management).")


# ===========================================================================
# 5. Case report
# ===========================================================================

class BenevolentCaseReportComponent(ComponentSection):
    key = "benevolent_cases"
    title = "Cases"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.models import BenevolentCase
        scheme = _scheme_filter_value(filters)
        qs = (BenevolentCase.objects.select_related("scheme", "event_type", "membership__member")
              .order_by("-event_date"))
        if scheme is not None:
            qs = qs.filter(scheme=scheme)
        if ctx.start:
            qs = qs.filter(event_date__gte=ctx.start)
        if ctx.end:
            qs = qs.filter(event_date__lte=ctx.end)
        columns = [Column("number", "Case", drilldown=True), Column("scheme", "Scheme"),
                  Column("beneficiary", "Beneficiary"), Column("event_type", "Event"),
                  Column("event_date", "Date"), Column("status", "Status"),
                  Column("approved", "Approved", numeric=True),
                  Column("paid", "Paid", numeric=True)]
        rows = [Row(cells={
            "number": c.number, "scheme": c.scheme.code,
            "beneficiary": c.beneficiary_display, "event_type": c.event_type.name,
            "event_date": c.event_date, "status": c.get_status_display(),
            "approved": c.approved_amount or Decimal(0), "paid": c.paid_total},
            url=f"/benevolent/cases/{c.pk}/") for c in qs[:2000]]
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           note=(f"{qs.count()} case(s) in the period"
                                + (" (showing the first 2,000 — export for the rest)"
                                   if qs.count() > 2000 else ".")))


# ===========================================================================
# 6. Fund balances & financial statement
# ===========================================================================

class BenevolentFundBalancesComponent(ComponentSection):
    key = "benevolent_fund_balances"
    title = "Fund balances"
    declared_metrics = ("benevolent_fund_balance",)

    def render(self, ctx, filters):
        from benevolent.models import BenevolentScheme
        scheme = _scheme_filter_value(filters)
        schemes = ([scheme] if scheme is not None
                  else list(BenevolentScheme.objects.exclude(
                      status=BenevolentScheme.Status.DRAFT)))
        columns = [Column("scheme", "Scheme"), Column("fund", "Fund"),
                  Column("balance", "Balance", numeric=True, drilldown=True)]
        rows = [Row(cells={"scheme": s.name, "fund": s.fund.name,
                          "balance": ctx.metric("benevolent_fund_balance", s)},
                   url=f"/reports/fund/{s.fund_id}/") for s in schemes]
        total = sum((r.cells["balance"] for r in rows), Decimal(0))
        total_row = Row(cells={"scheme": "TOTAL", "fund": "", "balance": total},
                        emphasis=True) if rows else None
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           total=total_row,
                           note="Each balance is the scheme's fund balance from the "
                                "Financial Metrics Registry — a scheme has no "
                                "separately-maintained figure of its own.")


class BenevolentIncomeExpenditureComponent(ComponentSection):
    key = "benevolent_income_expenditure"
    title = "Income & expenditure"
    declared_metrics = ("benevolent_contributions", "benevolent_payouts")

    def render(self, ctx, filters):
        scheme = _scheme_filter_value(filters)
        income = ctx.metric("benevolent_contributions", ctx.start, ctx.end, scheme)
        payouts = ctx.metric("benevolent_payouts", ctx.start, ctx.end, scheme)
        net = income - payouts
        columns = [Column("label", "Label"), Column("value", "Amount", numeric=True)]
        rows = [
            Row(cells={"label": "Contributions received", "value": income}),
            Row(cells={"label": "Benefits paid", "value": -payouts}),
            Row(cells={"label": "Net movement", "value": net}, emphasis=True),
        ]
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           note="Both figures are the same ones the module's own KPI "
                                "band and the general ledger report for these funds.")


# ===========================================================================
# 7. Arrears analysis
# ===========================================================================

class BenevolentArrearsComponent(ComponentSection):
    key = "benevolent_arrears_analysis"
    title = "Arrears analysis"
    declared_metrics = ("benevolent_arrears",)

    def render(self, ctx, filters):
        from benevolent.services.reporting import arrears_analysis
        scheme = _scheme_filter_value(filters)
        rows_data = arrears_analysis(scheme, ctx.end)
        columns = [Column("member", "Member", drilldown=True), Column("scheme", "Scheme"),
                  Column("owed", "Owed", numeric=True), Column("band", "Ageing")]
        rows = [Row(cells={"member": r["membership"].member.name, "scheme": r["scheme"].code,
                          "owed": r["owed"], "band": r["band"]},
                   url=f"/benevolent/members/{r['membership'].pk}/") for r in rows_data]
        total = sum((r["owed"] for r in rows_data), Decimal(0))
        total_row = Row(cells={"member": "TOTAL", "scheme": "", "owed": total, "band": ""},
                        emphasis=True) if rows else None
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           total=total_row,
                           note="Agrees with benevolent_arrears in the Financial Metrics "
                                "Registry — this is that total broken down member by "
                                "member, not a second calculation of it.")


# ===========================================================================
# 8. Benefit payments
# ===========================================================================

class BenevolentBenefitPaymentsComponent(ComponentSection):
    key = "benevolent_benefit_payments"
    title = "Benefit payments"
    declared_metrics = ("benevolent_payouts",)

    def render(self, ctx, filters):
        from benevolent.models import BenevolentPayout
        scheme = _scheme_filter_value(filters)
        qs = (BenevolentPayout.objects.select_related("case__scheme", "expense")
              .order_by("-expense__date"))
        if scheme is not None:
            qs = qs.filter(case__scheme=scheme)
        if ctx.start:
            qs = qs.filter(expense__date__gte=ctx.start)
        if ctx.end:
            qs = qs.filter(expense__date__lte=ctx.end)
        columns = [Column("date", "Date"), Column("case", "Case", drilldown=True),
                  Column("scheme", "Scheme"), Column("payee", "Payee"),
                  Column("amount", "Amount", numeric=True), Column("status", "Status")]
        rows = [Row(cells={"date": p.date, "case": p.case.number, "scheme": p.case.scheme.code,
                          "payee": p.payee_name, "amount": p.amount, "status": p.status},
                   url=f"/benevolent/cases/{p.case_id}/") for p in qs[:2000]]
        effective_total = sum((p.amount for p in qs if p.effective), Decimal(0))
        total_row = Row(cells={"date": "", "case": "TOTAL PAID", "scheme": "", "payee": "",
                               "amount": effective_total, "status": ""},
                        emphasis=True) if rows else None
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           total=total_row,
                           note="Only vouchers that have actually cleared (APPROVED or "
                                "PAID) count towards the total — a pending voucher is "
                                "shown but not summed, exactly as the case screens "
                                "themselves treat it.")


# ===========================================================================
# 9. Audit report
# ===========================================================================

class BenevolentAuditComponent(ComponentSection):
    key = "benevolent_audit"
    title = "Overrides & exceptions"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.services.reporting import audit_summary
        scheme = _scheme_filter_value(filters)
        data = audit_summary(scheme, ctx.start, ctx.end)
        columns = [Column("kind", "Kind"), Column("who", "Who"),
                  Column("scheme", "Scheme"), Column("detail", "Detail"),
                  Column("approved_by", "Approved by")]
        rows = []
        for c in data["overridden_cases"]:
            rows.append(Row(cells={
                "kind": "Case override", "who": c.beneficiary_display,
                "scheme": c.scheme.code, "detail": c.override_reason[:100],
                "approved_by": str(c.approved_by) if c.approved_by else "—"},
                url=f"/benevolent/cases/{c.pk}/"))
        for e in data["exemptions"]:
            rows.append(Row(cells={
                "kind": f"Exemption ({e.get_kind_display()})",
                "who": e.membership.member.name, "scheme": e.membership.scheme.code,
                "detail": e.reason[:100],
                "approved_by": str(e.approved_by) if e.approved_by else "—"},
                url=f"/benevolent/members/{e.membership_id}/"))
        for a in data["adjustments"]:
            rows.append(Row(cells={
                "kind": f"{a.get_kind_display()}", "who": a.membership.member.name,
                "scheme": a.membership.scheme.code, "detail": a.reason[:100],
                "approved_by": str(a.approved_by) if a.approved_by else "—"},
                url=f"/benevolent/members/{a.membership_id}/"))
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           note=f"{len(rows)} exceptional decision(s) in the period — the "
                                "same rows the Overrides & Exceptions screen shows, "
                                "reused rather than re-queried.")


# ===========================================================================
# Registration
# ===========================================================================

def register_components():
    """Register every component above with the shared Component Registry, so
    the Report Designer can compose them freely and the reports below can
    reuse them. Idempotent guard mirrors every other register_components()
    in the codebase — app reloads must not crash on a duplicate key."""
    entries = [
        ("benevolent_kpis", BenevolentKpiComponent, "Benevolent: key figures"),
        ("benevolent_scheme_summary_table", BenevolentSchemeSummaryComponent,
         "Benevolent: scheme summary"),
        ("benevolent_case_stats", BenevolentCaseStatsComponent, "Benevolent: cases by status"),
        ("benevolent_contribution_summary", BenevolentContributionSummaryComponent,
         "Benevolent: contribution summary"),
        ("benevolent_membership", BenevolentMembershipComponent, "Benevolent: membership"),
        ("benevolent_households", BenevolentHouseholdComponent, "Benevolent: households"),
        ("benevolent_committee", BenevolentCommitteeComponent, "Benevolent: committee"),
        ("benevolent_cases", BenevolentCaseReportComponent, "Benevolent: cases"),
        ("benevolent_fund_balances", BenevolentFundBalancesComponent,
         "Benevolent: fund balances"),
        ("benevolent_income_expenditure", BenevolentIncomeExpenditureComponent,
         "Benevolent: income & expenditure"),
        ("benevolent_arrears_analysis", BenevolentArrearsComponent, "Benevolent: arrears"),
        ("benevolent_benefit_payments", BenevolentBenefitPaymentsComponent,
         "Benevolent: benefit payments"),
        ("benevolent_audit", BenevolentAuditComponent, "Benevolent: audit"),
    ]
    for key, cls, label in entries:
        if not component_registry.has(key):
            component_registry.register(
                key, lambda _cls=cls, **k: _cls(**k), label=label, category="Benevolent")


def register_reports():
    """Register the ready-to-use reports the brief names — each composed
    from the components above, filterable by scheme (and date, where the
    figures are period-based), permissioned, and immediately exported to
    CSV/XLSX/PDF/DOCX/print with no further code, courtesy of the engine
    every other report in the system already uses."""
    if registry.get("benevolent_overview") is not None:
        return   # already registered this process (idempotent for app reloads)

    registry.register(Report(
        key="benevolent_overview", title="Benevolent: Overview & KPIs",
        description="Operational dashboard — headline figures, scheme summary, and "
                    "cases by status, for one scheme or every scheme at once.",
        category="Benevolent", permission=_can_view_benevolent,
        filters=[SCHEME_FILTER],
        sections=[BenevolentKpiComponent(), BenevolentSchemeSummaryComponent(),
                 BenevolentCaseStatsComponent()]))

    registry.register(Report(
        key="benevolent_contributions", title="Benevolent: Contribution Summary",
        description="Contributions received, by kind, for the period.",
        category="Benevolent", permission=_can_view_benevolent,
        filters=[SCHEME_FILTER],
        sections=[BenevolentContributionSummaryComponent()]))

    registry.register(Report(
        key="benevolent_membership_households", title="Benevolent: Membership & Households",
        description="The membership register and the household dimension within it.",
        category="Benevolent", permission=_can_view_benevolent,
        filters=[SCHEME_FILTER], period_from_request=False,
        sections=[BenevolentMembershipComponent(), BenevolentHouseholdComponent()]))

    registry.register(Report(
        key="benevolent_committee_report", title="Benevolent: Committee Report",
        description="Every scheme's committee roster and how active each seat has been.",
        category="Benevolent", permission=_can_view_benevolent,
        filters=[SCHEME_FILTER], period_from_request=False,
        sections=[BenevolentCommitteeComponent()]))

    registry.register(Report(
        key="benevolent_case_report", title="Benevolent: Case Report",
        description="Every case in the period, its status, and what it has been "
                    "approved and paid.",
        category="Benevolent", permission=_can_view_benevolent,
        filters=[SCHEME_FILTER],
        sections=[BenevolentCaseReportComponent()]))

    registry.register(Report(
        key="benevolent_financial_statement", title="Benevolent: Financial Statement",
        description="Fund balances and income & expenditure for the benevolent funds.",
        category="Benevolent", permission=_can_view_benevolent,
        filters=[SCHEME_FILTER],
        sections=[BenevolentFundBalancesComponent(), BenevolentIncomeExpenditureComponent()]))

    registry.register(Report(
        key="benevolent_arrears_report", title="Benevolent: Arrears Analysis",
        description="Every member currently in arrears, with a simple ageing band.",
        category="Benevolent", permission=_can_view_benevolent,
        filters=[SCHEME_FILTER], period_from_request=False,
        sections=[BenevolentArrearsComponent()]))

    registry.register(Report(
        key="benevolent_benefit_payments_report", title="Benevolent: Benefit Payments",
        description="Every payment voucher raised against a case, cleared or pending.",
        category="Benevolent", permission=_can_view_benevolent,
        filters=[SCHEME_FILTER],
        sections=[BenevolentBenefitPaymentsComponent()]))

    registry.register(Report(
        key="benevolent_audit_report", title="Benevolent: Audit Report",
        description="Every exceptional decision — case overrides, exemptions, charges "
                    "and waivers — in one place, for board and external review.",
        category="Benevolent", permission=_can_manage_benevolent,
        filters=[SCHEME_FILTER],
        sections=[BenevolentAuditComponent()]))
