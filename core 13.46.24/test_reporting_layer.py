"""Tests for the Semantic Reporting Layer (ReportContext) and the Generic
Report Engine, plus the request-scoped memoization that addresses
recommendation #1.

These are targeted at the new modules only. They assert three things the phase
promised: (1) the context draws from the registry and memoizes per render;
(2) the engine's pipeline (registration → filters → permission → shared context
→ sections → exports → drill-down) works end to end; (3) shared aggregates
compute once per request.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.metrics import metrics
from core.perfcache import request_scope, cached
from core.reporting import (Column, Filter, FunctionSection, Report,
                            ReportContext, Row, Section, SectionData,
                            registry)
from core.roles import TREASURER, AUDITOR
from departments.models import Department
from giving.models import Transaction


def _staff(username, role=TREASURER):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


# ===========================================================================
# Semantic Reporting Layer
# ===========================================================================

class ReportContextTests(TestCase):
    def setUp(self):
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        self.dev = Department.objects.create(name="Development", fund_type="LOCAL")
        for amt, dept in [("1000", self.tithe), ("2000", self.dev)]:
            Transaction.objects.create(
                date=dt.date(2026, 3, 1), channel="BANK", direction="CREDIT",
                amount=Decimal(amt), department=dept, allocation_status="AUTO",
                confirmed=True)

    def test_metric_draws_from_registry_and_applies_period(self):
        ctx = ReportContext.for_period(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        from reports.services import balances
        self.assertEqual(ctx.metric("tithe"),
                         balances.tithe_total(ctx.start, ctx.end))
        self.assertEqual(ctx.tithe(), Decimal("1000"))

    def test_unknown_metric_raises_keyerror(self):
        ctx = ReportContext()
        with self.assertRaises(KeyError):
            ctx.metric("not_a_metric")

    def test_memoizes_per_context(self):
        ctx = ReportContext.for_period(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        # patch the registered impl to count calls
        calls = {"n": 0}
        real = metrics._impl["fund_summary"]

        def counting(*a, **k):
            calls["n"] += 1
            return real(*a, **k)
        metrics._impl["fund_summary"] = counting
        try:
            ctx.fund_summary()
            ctx.fund_summary()
            ctx.metric("fund_summary", ctx.start, ctx.end, consolidated=True)
        finally:
            metrics._impl["fund_summary"] = real
        self.assertEqual(calls["n"], 1)   # computed once, served three times

    def test_distinct_args_cached_separately(self):
        ctx = ReportContext()
        a = ctx.metric("fund_summary", None, None, consolidated=True)
        b = ctx.metric("fund_summary", None, None, consolidated=False)
        self.assertIsNot(a, b)            # different args -> separate entries

    def test_metrics_used_tracks_provenance(self):
        ctx = ReportContext()
        ctx.tithe(); ctx.total_income(); ctx.tithe()
        self.assertEqual(ctx.metrics_used(), ["tithe", "total_income"])

    def test_from_request_reads_period(self):
        rf = self.client
        # a GET with start/end should flow into the context period
        u = _staff("ctx_req")
        self.client.force_login(u)
        # build via a tiny throwaway request object
        from django.test import RequestFactory
        req = RequestFactory().get("/x?start=2026-02-01&end=2026-02-28")
        ctx = ReportContext.from_request(req)
        self.assertEqual(ctx.start, dt.date(2026, 2, 1))
        self.assertEqual(ctx.end, dt.date(2026, 2, 28))


# ===========================================================================
# Request-scoped memoization (recommendation #1)
# ===========================================================================

class RequestScopeMemoTests(TestCase):
    def test_same_key_computes_once_within_scope(self):
        calls = {"n": 0}

        def compute():
            calls["n"] += 1
            return calls["n"]
        with request_scope():
            a = cached("k", compute)
            b = cached("k", compute)
        self.assertEqual((a, b, calls["n"]), (1, 1, 1))

    def test_scopes_are_isolated(self):
        calls = {"n": 0}

        def compute():
            calls["n"] += 1
            return calls["n"]
        with request_scope():
            cached("k", compute)
        with request_scope():
            cached("k", compute)      # new scope -> recompute
        self.assertEqual(calls["n"], 2)

    def test_no_scope_means_no_memo(self):
        calls = {"n": 0}

        def compute():
            calls["n"] += 1
            return calls["n"]
        cached("k", compute)
        cached("k", compute)
        self.assertEqual(calls["n"], 2)   # outside a scope, always computes

    def test_department_summary_deduped_across_sections(self):
        """collections_summary and local_funds_statement both call
        department_summary(s, e); within one request they compute it once."""
        from reports.services import treasurer as T, balances
        Department.objects.create(name="Dev", fund_type="LOCAL")
        as_of = dt.date(2026, 3, 15)
        s, e = T.month_bounds(as_of)
        calls = {"n": 0}
        real = balances._department_summary_impl

        def counting(start=None, end=None, consolidated=True):
            calls["n"] += 1
            return real(start, end, consolidated)
        balances._department_summary_impl = counting
        try:
            with request_scope():
                T.collections_summary(s, e)
                T.local_funds_statement(s, e)
        finally:
            balances._department_summary_impl = real
        self.assertEqual(calls["n"], 1)   # was 2 before the shared scope


# ===========================================================================
# Generic Report Engine
# ===========================================================================

class _CountSection(Section):
    key = "count"
    title = "Row count"

    def build(self, ctx, filters):
        n = len(ctx.fund_summary())
        return SectionData(
            key=self.key, title=self.title,
            columns=[Column("label", "Metric"), Column("value", "Value", numeric=True)],
            rows=[Row(cells={"label": "Funds", "value": n})], kind="table")


class EngineTests(TestCase):
    def setUp(self):
        Department.objects.create(name="Dev", fund_type="LOCAL")
        self.tr = _staff("eng_tr")

    def test_registration_and_lookup(self):
        rep = Report(key="t_reg", title="T", sections=[_CountSection()])
        registry.register(rep)
        try:
            self.assertIs(registry.get("t_reg"), rep)
            self.assertIn(rep, registry.all())
        finally:
            registry._reports.pop("t_reg", None)

    def test_duplicate_registration_rejected(self):
        rep = Report(key="t_dup", title="T", sections=[])
        registry.register(rep)
        try:
            with self.assertRaises(ValueError):
                registry.register(Report(key="t_dup", title="T2", sections=[]))
        finally:
            registry._reports.pop("t_dup", None)

    def test_render_pipeline_builds_shared_context(self):
        from django.test import RequestFactory
        seen = {}

        def build(ctx, filters):
            seen["ctx"] = ctx
            return SectionData("s", "S", [Column("a", "A")], [Row(cells={"a": 1})])

        rep = Report(key="t_share", title="Shared",
                     sections=[FunctionSection("s1", "S1", build),
                               FunctionSection("s2", "S2", build)])
        registry.register(rep)
        try:
            req = RequestFactory().get("/x")
            req.user = self.tr
            rendered = rep.render(req)
            self.assertEqual(len(rendered.sections), 2)
            # both sections received the SAME context instance
            self.assertIs(seen["ctx"], rendered.context)
        finally:
            registry._reports.pop("t_share", None)

    def test_permission_enforced(self):
        from django.test import RequestFactory
        from core.reporting import PermissionDenied_
        rep = Report(key="t_perm", title="P", sections=[],
                     permission=lambda u: False)
        registry.register(rep)
        try:
            req = RequestFactory().get("/x")
            req.user = self.tr
            with self.assertRaises(PermissionDenied_):
                rep.render(req)
        finally:
            registry._reports.pop("t_perm", None)

    def test_filter_resolution(self):
        f = Filter("consolidated", "Consolidate", kind="bool", default=True)
        from django.test import RequestFactory
        self.assertTrue(f.resolve(RequestFactory().get("/x")))       # default
        self.assertFalse(f.resolve(RequestFactory().get("/x?consolidated=0")))
        df = Filter("d", "D", kind="date")
        self.assertEqual(df.resolve(RequestFactory().get("/x?d=2026-05-01")),
                         dt.date(2026, 5, 1))

    def test_section_visibility(self):
        s = _CountSection()
        s.permission = lambda u: False
        self.assertFalse(s.visible_to(self.tr))
        s.permission = None
        self.assertTrue(s.visible_to(self.tr))


# ===========================================================================
# The registered demonstration report (fund_overview) end to end
# ===========================================================================

class FundOverviewReportTests(TestCase):
    def setUp(self):
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        self.dev = Department.objects.create(name="Development", fund_type="LOCAL")
        Transaction.objects.create(
            date=dt.date(2026, 3, 1), channel="BANK", direction="CREDIT",
            amount=Decimal("2500"), department=self.dev, allocation_status="AUTO",
            confirmed=True)
        self.tr = _staff("fo_tr")
        self.aud = _staff("fo_aud", AUDITOR)

    def test_registered(self):
        self.assertIsNotNone(registry.get("fund_overview"))

    def test_html_render_with_sections_and_drilldown(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["fund_overview"]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Fund balances")
        self.assertContains(r, "Income by channel")
        self.assertContains(r, "still to remit")
        # drill-down link to a fund ledger present
        self.assertContains(r, reverse("report_fund", args=[self.dev.id]))

    def test_auditor_can_view(self):
        self.client.force_login(self.aud)
        r = self.client.get(reverse("engine_report", args=["fund_overview"]))
        self.assertEqual(r.status_code, 200)

    def test_csv_and_xlsx_exports(self):
        self.client.force_login(self.tr)
        base = reverse("engine_report", args=["fund_overview"])
        rc = self.client.get(base + "?export=csv")
        self.assertEqual(rc.status_code, 200)
        self.assertIn("text/csv", rc["Content-Type"])
        body = rc.content.decode()
        self.assertIn("Fund balances", body)
        rx = self.client.get(base + "?export=xlsx")
        self.assertIn("spreadsheetml", rx["Content-Type"])

    def test_unknown_report_404(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["nope"]))
        self.assertEqual(r.status_code, 404)

    def test_consolidated_filter_flows_through(self):
        self.client.force_login(self.tr)
        r = self.client.get(reverse("engine_report", args=["fund_overview"])
                            + "?consolidated=0")
        self.assertEqual(r.status_code, 200)
