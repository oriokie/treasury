"""Item 6 — the fourteen reporting-gap reports.

These fill the gaps the brief named that the Phase-8 components (in
``report_components.py``) did not already cover: contribution compliance, ageing
arrears bands, pending approvals, rejected-case reasons, benefit utilisation,
scheme profitability, household statistics, dependant demographics, case
turnaround, committee performance, fraud alerts, missing documents, contribution
forecasting and fund sustainability.

Every one is a ``ComponentSection`` returning a ``SectionData``, exactly like the
Phase-8 components — so each gets an HTML page, CSV/XLSX/PDF/DOCX export, a print
view, permission enforcement and a place in the report library FOR FREE, with no
rendering code in this file. Where a report is worth a picture (a compliance
gauge, a utilisation bar, a forecast line, a demographic doughnut), the component
returns a second ``chart`` section built from the engine's own ``ChartSpec`` in
the app's forest/brass palette — the same charts the dashboards use, not a new
charting path.

No component computes a new financial figure. Money still comes from the registry
(via the reporting service's ``scheme_summary`` / solvency service), and these add
only the operational DIMENSIONS the fund tables cannot know — a percentage, a
count of days, an age band, a reason for a refusal.
"""
from __future__ import annotations

from decimal import Decimal

from core.reporting.charts import ChartSpec
from core.reporting.components import ComponentSection, component_registry
from core.reporting.engine import Column, Filter, Report, Row, SectionData, registry

from benevolent.report_components import (ACTIVE_FILTER, SCHEME_FILTER,
                                          _can_manage_benevolent,
                                          _can_view_benevolent, _n,
                                          _scheme_filter_value)


def _pct(v):
    return f"{v:.0f}%"


def _days(v):
    return "—" if v is None else f"{v:g} d"


# ===========================================================================
# 1. Contribution compliance
# ===========================================================================

class ContributionComplianceComponent(ComponentSection):
    key = "benevolent_contribution_compliance"
    title = "Contribution compliance"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.services.reporting import contribution_compliance
        scheme = _scheme_filter_value(filters)
        rows_data = contribution_compliance(scheme, ctx.end)
        if not rows_data:
            return SectionData(key=self.key, title=self.title,
                               columns=[Column("member", "Member")], rows=[],
                               note="No dues-based scheme in scope — compliance is a "
                                    "dues-scheme measure.")
        n = len(rows_data)
        fully = sum(1 for r in rows_data if r["compliance"] >= 100)
        avg = (sum((r["compliance"] for r in rows_data), Decimal(0)) / n).quantize(Decimal("1"))
        cards = SectionData(
            key=self.key + "_kpi", title="Compliance at a glance",
            columns=[Column("label", "Metric"), Column("value", "Value")],
            rows=[
                Row(cells={"label": "Members tracked", "value": n,
                           "display": str(n)}),
                Row(cells={"label": "Fully compliant", "value": fully,
                           "display": f"{fully}", "sub": f"{fully/n*100:.0f}% of members"}),
                Row(cells={"label": "Average compliance", "value": avg,
                           "display": _pct(avg)}),
            ], kind="kpi")

        columns = [Column("member", "Member", drilldown=True), Column("scheme", "Scheme"),
                   Column("paid", "Periods paid", numeric=True),
                   Column("due", "Periods due", numeric=True),
                   Column("missed", "Missed", numeric=True),
                   Column("compliance", "Compliance", numeric=True)]
        rows = [Row(cells={
            "member": r["membership"].member.name, "scheme": r["scheme"].code,
            "paid": r["paid"], "due": r["due"], "missed": r["missed"],
            "compliance": r["compliance"]},
            url=f"/benevolent/members/{r['membership'].pk}/",
            emphasis=(r["compliance"] < 50)) for r in rows_data]
        table = SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                            note="Worst compliance first. A period counts as paid only "
                                 "when fully settled — the same rule the arrears figure "
                                 "uses, so the two always agree.")
        self._chart = ChartSpec(
            key=self.key + "_chart", chart_type="doughnut",
            labels=["Fully compliant", "Behind"],
            datasets=[{"data": [fully, n - fully]}],
            title="Members fully compliant")
        return [cards, self._make_chart_section(), table]

    def _make_chart_section(self):
        return SectionData(key=self._chart.key, title="", columns=[], rows=[],
                           kind="chart", extra={"chart": self._chart.to_config()})


# ===========================================================================
# 2. Ageing arrears
# ===========================================================================

class AgeingArrearsComponent(ComponentSection):
    key = "benevolent_ageing_arrears"
    title = "Ageing arrears"
    declared_metrics = ("benevolent_arrears",)

    def render(self, ctx, filters):
        from benevolent.services.reporting import arrears_analysis
        scheme = _scheme_filter_value(filters)
        rows_data = arrears_analysis(scheme, ctx.end)
        bands = {}
        band_totals = {}
        for r in rows_data:
            b = r["band"]
            bands[b] = bands.get(b, 0) + 1
            band_totals[b] = band_totals.get(b, Decimal(0)) + _n(r["owed"])
        order = ["Current", "1 period", "2 periods", "3+ periods", "Long overdue"]
        seen = [b for b in order if b in bands] + [b for b in bands if b not in order]

        summary_cols = [Column("band", "Ageing band"),
                        Column("members", "Members", numeric=True),
                        Column("owed", "Owed", numeric=True)]
        summary_rows = [Row(cells={"band": b, "members": bands[b],
                                   "owed": band_totals[b]}) for b in seen]
        total_owed = sum(band_totals.values(), Decimal(0))
        summary_total = Row(cells={"band": "TOTAL", "members": len(rows_data),
                                   "owed": total_owed}, emphasis=True) if rows_data else None
        summary = SectionData(key=self.key + "_bands", title="Arrears by age",
                              columns=summary_cols, rows=summary_rows,
                              total=summary_total,
                              note="Agrees with benevolent_arrears in the Financial "
                                   "Metrics Registry — the same total, aged into bands.")

        chart = ChartSpec(key=self.key + "_chart", chart_type="bar",
                          labels=seen,
                          datasets=[{"label": "Amount owed",
                                     "data": [float(band_totals[b]) for b in seen]}],
                          title="Owed by ageing band")
        chart_section = SectionData(key=chart.key, title="", columns=[], rows=[],
                                    kind="chart", extra={"chart": chart.to_config()})

        detail_cols = [Column("member", "Member", drilldown=True), Column("scheme", "Scheme"),
                       Column("owed", "Owed", numeric=True), Column("band", "Ageing")]
        detail_rows = [Row(cells={"member": r["membership"].member.name,
                                  "scheme": r["scheme"].code, "owed": r["owed"],
                                  "band": r["band"]},
                           url=f"/benevolent/members/{r['membership'].pk}/")
                       for r in rows_data]
        detail = SectionData(key=self.key, title="Members in arrears",
                             columns=detail_cols, rows=detail_rows,
                             note=f"{len(rows_data)} member(s) currently in arrears.")
        return [summary, chart_section, detail]


# ===========================================================================
# 3. Pending approvals
# ===========================================================================

class PendingApprovalsComponent(ComponentSection):
    key = "benevolent_pending_approvals"
    title = "Pending approvals & payments"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.models import BenevolentCase
        from benevolent.services.reporting import pending_approvals
        scheme = _scheme_filter_value(filters)
        cases = pending_approvals(scheme)
        stage = {"await_assess": 0, "await_approve": 0, "await_pay": 0}
        for c in cases:
            if c.status == BenevolentCase.Status.SUBMITTED:
                stage["await_assess"] += 1
            elif c.status == BenevolentCase.Status.ASSESSED:
                stage["await_approve"] += 1
            else:
                stage["await_pay"] += 1
        cards = SectionData(
            key=self.key + "_kpi", title="Awaiting action",
            columns=[Column("label", "Stage"), Column("value", "Cases")],
            rows=[
                Row(cells={"label": "Awaiting assessment", "value": stage["await_assess"],
                           "display": str(stage["await_assess"])}),
                Row(cells={"label": "Awaiting approval", "value": stage["await_approve"],
                           "display": str(stage["await_approve"])}),
                Row(cells={"label": "Approved, awaiting payment", "value": stage["await_pay"],
                           "display": str(stage["await_pay"])}),
            ], kind="kpi")

        columns = [Column("number", "Case", drilldown=True), Column("scheme", "Scheme"),
                   Column("beneficiary", "Beneficiary"), Column("event", "Event"),
                   Column("event_date", "Event date"), Column("status", "Waiting on"),
                   Column("claimed", "Claimed", numeric=True)]
        rows = [Row(cells={
            "number": c.number, "scheme": c.scheme.code,
            "beneficiary": c.beneficiary_display, "event": c.event_type.name,
            "event_date": c.event_date, "status": c.get_status_display(),
            "claimed": c.claimed_amount or Decimal(0)},
            url=f"/benevolent/cases/{c.pk}/") for c in cases]
        table = SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                            note=f"{len(cases)} case(s) awaiting a decision or payment, "
                                 "oldest event first.")
        return [cards, table]


# ===========================================================================
# 4. Rejected reasons
# ===========================================================================

class RejectedReasonsComponent(ComponentSection):
    key = "benevolent_rejected_reasons"
    title = "Rejected cases & reasons"
    declared_metrics = ()

    def render(self, ctx, filters):
        scheme = _scheme_filter_value(filters)
        from benevolent.services.reporting import rejected_reasons
        cases = rejected_reasons(scheme, ctx.start, ctx.end)
        columns = [Column("number", "Case", drilldown=True), Column("scheme", "Scheme"),
                   Column("beneficiary", "Beneficiary"), Column("event", "Event"),
                   Column("event_date", "Date"), Column("by", "Rejected by"),
                   Column("reason", "Reason")]
        rows = [Row(cells={
            "number": c.number, "scheme": c.scheme.code,
            "beneficiary": c.beneficiary_display, "event": c.event_type.name,
            "event_date": c.event_date,
            "by": (c.rejected_by.get_username() if c.rejected_by else "—"),
            "reason": c.rejection_reason or "—"},
            url=f"/benevolent/cases/{c.pk}/") for c in cases]
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           note=f"{len(cases)} case(s) rejected in the period.")


# ===========================================================================
# 5. Benefit utilisation
# ===========================================================================

class BenefitUtilisationComponent(ComponentSection):
    key = "benevolent_benefit_utilisation"
    title = "Benefit utilisation"
    declared_metrics = ("benevolent_contributions", "benevolent_payouts")

    def render(self, ctx, filters):
        from benevolent.services.reporting import benefit_utilisation
        scheme = _scheme_filter_value(filters)
        rows_data = benefit_utilisation(ctx.start, ctx.end)
        if scheme is not None:
            rows_data = [r for r in rows_data if r["scheme"].pk == scheme.pk]
        columns = [Column("scheme", "Scheme"),
                   Column("contributions", "Contributions in", numeric=True),
                   Column("payouts", "Benefits out", numeric=True),
                   Column("net", "Net", numeric=True),
                   Column("utilisation", "Utilisation %", numeric=True)]
        rows = [Row(cells={
            "scheme": r["scheme"].name, "contributions": r["contributions"],
            "payouts": r["payouts"], "net": r["net"],
            "utilisation": r["utilisation"]}) for r in rows_data]
        chart = ChartSpec(
            key=self.key + "_chart", chart_type="bar",
            labels=[r["scheme"].code for r in rows_data],
            datasets=[{"label": "Contributions", "data": [float(r["contributions"]) for r in rows_data]},
                      {"label": "Benefits paid", "data": [float(r["payouts"]) for r in rows_data]}],
            title="Contributions vs benefits by scheme")
        chart_section = SectionData(key=chart.key, title="", columns=[], rows=[],
                                    kind="chart", extra={"chart": chart.to_config()})
        table = SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                            note="Utilisation is benefits paid ÷ contributions received. "
                                 "Both money figures are the registry's — this only "
                                 "expresses one as a proportion of the other.")
        return [chart_section, table]


# ===========================================================================
# 6. Scheme profitability (surplus/deficit)
# ===========================================================================

class SchemeProfitabilityComponent(ComponentSection):
    key = "benevolent_scheme_profitability"
    title = "Scheme surplus / deficit"
    declared_metrics = ("benevolent_scheme_summary",)

    def render(self, ctx, filters):
        from benevolent.services.reporting import scheme_summary
        scheme = _scheme_filter_value(filters)
        rows_data = scheme_summary(ctx.start, ctx.end)
        if scheme is not None:
            rows_data = [r for r in rows_data if r["scheme"].pk == scheme.pk]
        columns = [Column("scheme", "Scheme"),
                   Column("contributions", "Contributions", numeric=True),
                   Column("payouts", "Benefits paid", numeric=True),
                   Column("surplus", "Surplus / (deficit)", numeric=True)]
        rows = []
        for r in rows_data:
            surplus = _n(r["contributions"]) - _n(r["payouts"])
            rows.append(Row(cells={
                "scheme": r["scheme"].name, "contributions": r["contributions"],
                "payouts": r["payouts"], "surplus": surplus},
                emphasis=(surplus < 0)))
        tot_c = sum((_n(r["contributions"]) for r in rows_data), Decimal(0))
        tot_p = sum((_n(r["payouts"]) for r in rows_data), Decimal(0))
        total = Row(cells={"scheme": "TOTAL", "contributions": tot_c, "payouts": tot_p,
                           "surplus": tot_c - tot_p}, emphasis=True) if rows_data else None
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           total=total,
                           note="Surplus is contributions received less benefits paid "
                                "for the period — an operating view, not the fund "
                                "balance (which also carries prior years' reserves).")


# ===========================================================================
# 7. Household statistics
# ===========================================================================

class HouseholdStatisticsComponent(ComponentSection):
    key = "benevolent_household_statistics"
    title = "Household statistics"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.services.reporting import household_statistics
        scheme = _scheme_filter_value(filters)
        stats = household_statistics(scheme)
        cards = SectionData(
            key=self.key + "_kpi", title="Households at a glance",
            columns=[Column("label", "Metric"), Column("value", "Value")],
            rows=[
                Row(cells={"label": "Households", "value": stats["households"],
                           "display": str(stats["households"])}),
                Row(cells={"label": "Average size", "value": stats["avg_size"],
                           "display": f"{stats['avg_size']:g}"}),
                Row(cells={"label": "Total dependants", "value": stats["total_dependants"],
                           "display": str(stats["total_dependants"])}),
            ], kind="kpi")
        dist = stats["size_distribution"]
        order = ["1", "2–3", "4–5", "6+"]
        seen = [b for b in order if b in dist]
        columns = [Column("band", "Household size"), Column("n", "Households", numeric=True)]
        rows = [Row(cells={"band": b, "n": dist[b]}) for b in seen]
        chart = ChartSpec(key=self.key + "_chart", chart_type="bar", labels=seen,
                          datasets=[{"label": "Households", "data": [dist[b] for b in seen]}],
                          title="Households by size")
        chart_section = SectionData(key=chart.key, title="", columns=[], rows=[],
                                    kind="chart", extra={"chart": chart.to_config()})
        table = SectionData(key=self.key, title="Size distribution", columns=columns,
                            rows=rows, note="A household is a member plus their registered "
                                            "dependants.")
        return [cards, chart_section, table]


# ===========================================================================
# 8. Dependant demographics
# ===========================================================================

class DependantDemographicsComponent(ComponentSection):
    key = "benevolent_dependant_demographics"
    title = "Dependant demographics"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.services.reporting import dependant_demographics
        scheme = _scheme_filter_value(filters)
        demo = dependant_demographics(scheme)

        rel = demo["by_relationship"]
        rel_rows = [Row(cells={"group": k, "n": v})
                    for k, v in sorted(rel.items(), key=lambda kv: -kv[1])]
        rel_section = SectionData(
            key=self.key + "_rel", title="By relationship",
            columns=[Column("group", "Relationship"), Column("n", "Count", numeric=True)],
            rows=rel_rows, note=f"{demo['total']} active dependant(s).")

        rel_chart = ChartSpec(
            key=self.key + "_relchart", chart_type="doughnut",
            labels=list(rel.keys()), datasets=[{"data": list(rel.values())}],
            title="Dependants by relationship")
        rel_chart_section = SectionData(key=rel_chart.key, title="", columns=[], rows=[],
                                        kind="chart", extra={"chart": rel_chart.to_config()})

        bands = demo["by_age_band"]
        band_order = ["0–5", "6–17", "18–35", "36–59", "60+", "unknown"]
        seen = [b for b in band_order if bands.get(b)]
        age_rows = [Row(cells={"band": b, "n": bands[b]}) for b in seen]
        age_section = SectionData(
            key=self.key, title="By age band",
            columns=[Column("band", "Age band"), Column("n", "Count", numeric=True)],
            rows=age_rows, note="Age is computed from recorded dates of birth; dependants "
                                "without one are shown as 'unknown'.")
        return [rel_section, rel_chart_section, age_section]


# ===========================================================================
# 9. Case turnaround
# ===========================================================================

class CaseTurnaroundComponent(ComponentSection):
    key = "benevolent_case_turnaround"
    title = "Case turnaround"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.services.reporting import case_turnaround
        scheme = _scheme_filter_value(filters)
        data = case_turnaround(scheme, ctx.start, ctx.end)
        cards = SectionData(
            key=self.key + "_kpi", title="Average turnaround",
            columns=[Column("label", "Stage"), Column("value", "Days")],
            rows=[
                Row(cells={"label": "Submit → assess", "value": 0,
                           "display": _days(data["avg_submit_to_assess"])}),
                Row(cells={"label": "Assess → approve", "value": 0,
                           "display": _days(data["avg_assess_to_approve"])}),
                Row(cells={"label": "Reported → paid", "value": 0,
                           "display": _days(data["avg_report_to_pay"]),
                           "sub": f"{data['n']} case(s)"}),
            ], kind="kpi")
        columns = [Column("case", "Case", drilldown=True), Column("scheme", "Scheme"),
                   Column("submit_to_assess", "Submit→assess (d)", numeric=True),
                   Column("assess_to_approve", "Assess→approve (d)", numeric=True),
                   Column("report_to_pay", "Reported→paid (d)", numeric=True)]
        rows = []
        for r in data["rows"]:
            c = r["case"]
            rows.append(Row(cells={
                "case": c.number, "scheme": c.scheme.code,
                "submit_to_assess": _round(r["submit_to_assess"]),
                "assess_to_approve": _round(r["assess_to_approve"]),
                "report_to_pay": r["report_to_pay"] if r["report_to_pay"] is not None else ""},
                url=f"/benevolent/cases/{c.pk}/"))
        table = SectionData(key=self.key, title="Per case", columns=columns, rows=rows,
                            note="Days at each stage, from the case's own lifecycle "
                                 "timestamps. Only cases that reached approval are shown.")
        return [cards, table]


def _round(v):
    return "" if v is None else round(v, 1)


# ===========================================================================
# 10. Committee performance
# ===========================================================================

class CommitteePerformanceComponent(ComponentSection):
    key = "benevolent_committee_performance"
    title = "Committee performance"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.services.reporting import committee_report
        scheme = _scheme_filter_value(filters)
        rows_data = committee_report(scheme)
        columns = [Column("scheme", "Scheme"), Column("person", "Member"),
                   Column("role", "Role"), Column("seated_since", "Seated since"),
                   Column("votes", "Decisions recorded", numeric=True)]
        rows = [Row(cells={
            "scheme": r["scheme"].code,
            "person": r["user"].get_full_name() or r["user"].username,
            "role": r["role"], "seated_since": r["seated_since"].date(),
            "votes": r["votes_cast"]},
            emphasis=(r["votes_cast"] == 0)) for r in rows_data]
        chart = None
        if rows_data:
            top = sorted(rows_data, key=lambda r: -r["votes_cast"])[:8]
            chart = ChartSpec(
                key=self.key + "_chart", chart_type="bar",
                labels=[(r["user"].get_full_name() or r["user"].username) for r in top],
                datasets=[{"label": "Decisions", "data": [r["votes_cast"] for r in top]}],
                title="Decisions recorded per member")
        sections = []
        if chart is not None:
            sections.append(SectionData(key=chart.key, title="", columns=[], rows=[],
                                        kind="chart", extra={"chart": chart.to_config()}))
        sections.append(SectionData(
            key=self.key, title=self.title, columns=columns, rows=rows,
            note="Members with no decisions recorded are highlighted — either newly "
                 "seated or inactive on the committee."))
        return sections


# ===========================================================================
# 11. Fraud alerts
# ===========================================================================

class FraudAlertsComponent(ComponentSection):
    key = "benevolent_fraud_alerts"
    title = "Fraud red flags"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.services import fraud as fraud_svc
        scheme = _scheme_filter_value(filters)
        signals = fraud_svc.scan(scheme=scheme)
        summ = {"high": 0, "medium": 0, "low": 0}
        for s in signals:
            summ[s.severity] = summ.get(s.severity, 0) + 1
        cards = SectionData(
            key=self.key + "_kpi", title="Red flags by severity",
            columns=[Column("label", "Severity"), Column("value", "Count")],
            rows=[Row(cells={"label": "High", "value": summ["high"], "display": str(summ["high"])}),
                  Row(cells={"label": "Medium", "value": summ["medium"], "display": str(summ["medium"])}),
                  Row(cells={"label": "Low", "value": summ["low"], "display": str(summ["low"])})],
            kind="kpi")
        columns = [Column("severity", "Severity"), Column("flag", "Flag"),
                   Column("detail", "Detail")]
        rows = [Row(cells={"severity": s.severity.title(), "label": s.label,
                           "flag": s.label, "detail": s.detail},
                    url=(f"/benevolent/cases/{s.case_id}/" if s.case_id else None),
                    emphasis=(s.severity == "high")) for s in signals]
        table = SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                            note="Every item is a signal for a human to judge, not a "
                                 "verdict — the same scan the Red flags screen shows.")
        return [cards, table]


# ===========================================================================
# 12. Missing documents ("document expiry")
# ===========================================================================

class MissingDocumentsComponent(ComponentSection):
    key = "benevolent_missing_documents"
    title = "Outstanding documents"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.services.reporting import missing_documents
        scheme = _scheme_filter_value(filters)
        rows_data = missing_documents(scheme, ctx.start, ctx.end)
        columns = [Column("number", "Case", drilldown=True), Column("scheme", "Scheme"),
                   Column("beneficiary", "Beneficiary"), Column("event", "Event"),
                   Column("missing", "Documents still needed")]
        rows = [Row(cells={
            "number": r["case"].number, "scheme": r["scheme"].code,
            "beneficiary": r["case"].beneficiary_display,
            "event": r["case"].event_type.name,
            "missing": ", ".join(r["missing"])},
            url=f"/benevolent/cases/{r['case'].pk}/", emphasis=True) for r in rows_data]
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           note="Open cases whose required supporting documents are not "
                                "all attached — the same check that blocks a case at "
                                "approval, surfaced early so paperwork can be chased.")


# ===========================================================================
# 13. Contribution forecasting
# ===========================================================================

class ContributionForecastComponent(ComponentSection):
    key = "benevolent_contribution_forecast"
    title = "Contribution & cash forecast"
    declared_metrics = ()

    def render(self, ctx, filters):
        from benevolent.models import BenevolentScheme
        from benevolent.services import solvency as sol
        scheme = _scheme_filter_value(filters)
        schemes = ([scheme] if scheme is not None
                   else list(BenevolentScheme.objects.filter(
                       status=BenevolentScheme.Status.ACTIVE)))
        columns = [Column("scheme", "Scheme"),
                   Column("balance", "Balance now", numeric=True),
                   Column("inflow", "Avg monthly in", numeric=True),
                   Column("outflow", "Avg monthly out", numeric=True),
                   Column("dry", "Projected to run dry")]
        rows = []
        chart_scheme = schemes[0] if len(schemes) == 1 else None
        for s in schemes:
            fc = sol.forecast_scheme(s, months=6)
            first = fc.months[0] if fc.months else None
            balance_now = first.opening if first else Decimal(0)
            inflow = first.inflow if first else Decimal(0)
            # outflow of month 1 includes approved-unpaid; use month 2 for the
            # steady rate where available
            steady = fc.months[1].outflow if len(fc.months) > 1 else (
                first.outflow if first else Decimal(0))
            dry = "No — sustainable"
            if fc.runs_dry and fc.first_dry_month:
                dry = f"Around {fc.first_dry_month}"
            rows.append(Row(cells={
                "scheme": s.name, "balance": balance_now, "inflow": inflow,
                "outflow": steady, "dry": dry}, emphasis=fc.runs_dry))
        sections = []
        if chart_scheme is not None:
            fc = sol.forecast_scheme(chart_scheme, months=6)
            chart = ChartSpec(
                key=self.key + "_chart", chart_type="line",
                labels=[m.month for m in fc.months],
                datasets=[{"label": "Projected closing balance",
                           "data": [float(m.closing) for m in fc.months]}],
                title=f"{chart_scheme.name}: projected balance")
            sections.append(SectionData(key=chart.key, title="", columns=[], rows=[],
                                        kind="chart", extra={"chart": chart.to_config()}))
        sections.append(SectionData(
            key=self.key, title=self.title, columns=columns, rows=rows,
            note="A straight-line projection at each scheme's trailing 6-month "
                 "run-rate — a planning aid, not a promise. Schemes projected to run "
                 "dry are highlighted."))
        return sections


# ===========================================================================
# 14. Fund sustainability
# ===========================================================================

class FundSustainabilityComponent(ComponentSection):
    key = "benevolent_fund_sustainability"
    title = "Fund sustainability"
    declared_metrics = ("benevolent_reserved_commitments",)

    def render(self, ctx, filters):
        from benevolent.models import BenevolentScheme
        from benevolent.services import solvency as sol
        scheme = _scheme_filter_value(filters)
        schemes = ([scheme] if scheme is not None
                   else list(BenevolentScheme.objects.filter(
                       status=BenevolentScheme.Status.ACTIVE)))
        columns = [Column("scheme", "Scheme"),
                   Column("balance", "Balance", numeric=True),
                   Column("approved_unpaid", "Approved unpaid", numeric=True),
                   Column("reserved", "Reserved (open cases)", numeric=True),
                   Column("free", "Free to commit", numeric=True),
                   Column("status", "Status")]
        rows = []
        for s in schemes:
            pos = sol.fund_position(s)
            if pos.is_negative:
                status = "Overdrawn"
            elif pos.is_overcommitted:
                status = "Over-committed"
            elif pos.is_depleted:
                status = "Fully committed"
            else:
                status = "Healthy"
            rows.append(Row(cells={
                "scheme": s.name, "balance": pos.balance,
                "approved_unpaid": pos.approved_unpaid,
                "reserved": pos.reserved_open_cases,
                "free": pos.available_after_reserved, "status": status},
                emphasis=(pos.is_depleted or pos.is_negative)))
        return SectionData(key=self.key, title=self.title, columns=columns, rows=rows,
                           note="'Free to commit' is the balance once approved-but-unpaid "
                                "benefits and money reserved for open cases are set aside. "
                                "Those set-asides are memoranda, not ledger liabilities — "
                                "the balance itself is the registry's figure.")


# ===========================================================================
# Registration
# ===========================================================================

_EXTRA = [
    ("benevolent_contribution_compliance", ContributionComplianceComponent,
     "Benevolent: contribution compliance"),
    ("benevolent_ageing_arrears", AgeingArrearsComponent, "Benevolent: ageing arrears"),
    ("benevolent_pending_approvals", PendingApprovalsComponent,
     "Benevolent: pending approvals"),
    ("benevolent_rejected_reasons", RejectedReasonsComponent,
     "Benevolent: rejected reasons"),
    ("benevolent_benefit_utilisation", BenefitUtilisationComponent,
     "Benevolent: benefit utilisation"),
    ("benevolent_scheme_profitability", SchemeProfitabilityComponent,
     "Benevolent: scheme surplus/deficit"),
    ("benevolent_household_statistics", HouseholdStatisticsComponent,
     "Benevolent: household statistics"),
    ("benevolent_dependant_demographics", DependantDemographicsComponent,
     "Benevolent: dependant demographics"),
    ("benevolent_case_turnaround", CaseTurnaroundComponent, "Benevolent: case turnaround"),
    ("benevolent_committee_performance", CommitteePerformanceComponent,
     "Benevolent: committee performance"),
    ("benevolent_fraud_alerts", FraudAlertsComponent, "Benevolent: fraud alerts"),
    ("benevolent_missing_documents", MissingDocumentsComponent,
     "Benevolent: outstanding documents"),
    ("benevolent_contribution_forecast", ContributionForecastComponent,
     "Benevolent: contribution forecast"),
    ("benevolent_fund_sustainability", FundSustainabilityComponent,
     "Benevolent: fund sustainability"),
]


def register_components():
    for key, cls, label in _EXTRA:
        if not component_registry.has(key):
            component_registry.register(
                key, lambda _cls=cls, **k: _cls(**k), label=label, category="Benevolent")


def register_reports():
    if registry.get("benevolent_contribution_compliance_report") is not None:
        return

    def _r(key, title, description, component, permission=None, filters=None,
           period=True):
        registry.register(Report(
            key=key, title=title, description=description, category="Benevolent",
            permission=permission or _can_view_benevolent,
            filters=filters if filters is not None else [SCHEME_FILTER],
            period_from_request=period, sections=[component()]))

    _r("benevolent_contribution_compliance_report", "Benevolent: Contribution Compliance",
       "Per member, how many dues periods were paid on time and the compliance %.",
       ContributionComplianceComponent, period=False)
    _r("benevolent_ageing_arrears_report", "Benevolent: Ageing Arrears",
       "Members in arrears, aged into bands, with the total owed per band.",
       AgeingArrearsComponent, period=False)
    _r("benevolent_pending_approvals_report", "Benevolent: Pending Approvals",
       "Every case awaiting assessment, approval or payment.",
       PendingApprovalsComponent, period=False)
    _r("benevolent_rejected_reasons_report", "Benevolent: Rejected Cases",
       "Rejected cases and the reason each was refused.", RejectedReasonsComponent)
    _r("benevolent_benefit_utilisation_report", "Benevolent: Benefit Utilisation",
       "Contributions in versus benefits out, by scheme, with a utilisation ratio.",
       BenefitUtilisationComponent)
    _r("benevolent_scheme_profitability_report", "Benevolent: Scheme Surplus/Deficit",
       "Each scheme's operating surplus or deficit for the period.",
       SchemeProfitabilityComponent)
    _r("benevolent_household_statistics_report", "Benevolent: Household Statistics",
       "How many households, their average size, and the size distribution.",
       HouseholdStatisticsComponent, period=False)
    _r("benevolent_dependant_demographics_report", "Benevolent: Dependant Demographics",
       "Dependants by relationship and by age band.", DependantDemographicsComponent,
       period=False)
    _r("benevolent_case_turnaround_report", "Benevolent: Case Turnaround",
       "How long cases take at each stage, from the lifecycle timestamps.",
       CaseTurnaroundComponent)
    _r("benevolent_committee_performance_report", "Benevolent: Committee Performance",
       "Each committee member's seat and how many decisions they have recorded.",
       CommitteePerformanceComponent, period=False)
    _r("benevolent_fraud_alerts_report", "Benevolent: Fraud Red Flags",
       "The red-flag scan across the schemes, ranked by severity.",
       FraudAlertsComponent, permission=_can_manage_benevolent, period=False)
    _r("benevolent_missing_documents_report", "Benevolent: Outstanding Documents",
       "Open cases whose required supporting documents are not yet all attached.",
       MissingDocumentsComponent, period=False)
    _r("benevolent_contribution_forecast_report", "Benevolent: Contribution Forecast",
       "A straight-line cash projection per scheme at its recent run-rate.",
       ContributionForecastComponent, period=False)
    _r("benevolent_fund_sustainability_report", "Benevolent: Fund Sustainability",
       "Each fund's balance, its commitments, and what is free to commit.",
       FundSustainabilityComponent, period=False)
