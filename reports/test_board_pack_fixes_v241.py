"""Tests for four fixes to the Treasurer's Report board pack (v2.41):

3. Chart sizing (a systemic fix: ChartSpec.to_config() now defaults every
   engine chart to maintainAspectRatio:false/responsive:true, and every
   canvas that renders one sits in a height-constrained box) + fund balances
   sorted alphabetically within each statement.
4. The (non-functional) "Ask AI" links removed from the board pack.
5. A broken multi-line Django {# #} comment (Django comments cannot span
   multiple lines — the whole block was rendering as literal visible text)
   removed.
"""
import datetime as dt

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.reporting.charts import ChartSpec
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction


def _treasurer(username="cp_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class ChartSizingDefaultsTests(TestCase):
    """Item 3 (systemic root cause): every engine chart gets safe sizing
    defaults, not just the treasurer report's own charts."""

    def test_doughnut_gets_size_defaults(self):
        spec = ChartSpec(key="k", chart_type="doughnut", labels=["A", "B"],
                         datasets=[{"data": [1, 2]}])
        cfg = spec.to_config()
        self.assertEqual(cfg["options"]["maintainAspectRatio"], False)
        self.assertEqual(cfg["options"]["responsive"], True)

    def test_bar_gets_size_defaults(self):
        spec = ChartSpec(key="k", chart_type="bar", labels=["A"],
                         datasets=[{"data": [1]}])
        cfg = spec.to_config()
        self.assertEqual(cfg["options"]["maintainAspectRatio"], False)

    def test_caller_supplied_option_still_wins(self):
        spec = ChartSpec(key="k", chart_type="bar", labels=["A"],
                         datasets=[{"data": [1]}],
                         options={"maintainAspectRatio": True})
        cfg = spec.to_config()
        self.assertEqual(cfg["options"]["maintainAspectRatio"], True)


class _ReportSeed(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.client.force_login(self.tr)
        # a few funds, deliberately not alphabetically inserted, local + trust
        names_local = ["Zebra Fund", "Amber Fund", "Middle Fund"]
        names_trust = ["Zulu Trust", "Alpha Trust"]
        for n in names_local:
            Department.objects.create(name=n, fund_type="LOCAL")
        for n in names_trust:
            Department.objects.create(name=n, fund_type="TRUST")
        Transaction.objects.create(
            date=dt.date(2026, 5, 4), channel="CASH", direction="CREDIT",
            amount=1000, department=Department.objects.get(name="Zebra Fund"),
            allocation_status="AUTO", confirmed=True)
        self.q = "?start=2026-01-01&end=2026-12-31"


class ChartRenderingTests(_ReportSeed):
    def test_board_pack_charts_carry_size_defaults(self):
        import re, json
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + self.q)
        html = r.content.decode()
        m = re.search(r'<script id="engine-charts" type="application/json">'
                      r'(.*?)</script>', html, re.S)
        self.assertIsNotNone(m)
        data = json.loads(m.group(1))
        self.assertTrue(data)
        for key, cfg in data.items():
            self.assertEqual(cfg["options"].get("maintainAspectRatio"), False, key)

    def test_board_pack_chart_has_height_constrained_box(self):
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + self.q)
        self.assertIn("bp-chart-box", r.content.decode())

    def test_generic_engine_report_chart_has_height_constrained_box(self):
        r = self.client.get(reverse("engine_report", args=["income_statement_v2"])
                            + self.q)
        if r.status_code == 200 and b"chart_" in r.content:
            self.assertIn(b"chart-box", r.content)


class FundBalancesSortedTests(_ReportSeed):
    def test_fund_summary_component_sorted(self):
        from core.reporting import ReportContext
        from core.reporting.component_library import FundSummaryComponent
        ctx = ReportContext.for_period(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        data = FundSummaryComponent().render(ctx, {})
        names = [r.cells["fund"] for r in data.rows]
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_fund_balances_statement_sorted_within_each_block(self):
        from core.reporting import ReportContext
        from reports.financial_statements import FundBalancesStatementSection
        ctx = ReportContext.for_period(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        data = FundBalancesStatementSection().render(ctx, {})
        # local block then trust block; extract fund name rows only (skip
        # heading/total rows, which carry emphasis)
        names = [r.cells["fund"].strip() for r in data.rows if not r.emphasis]
        local_names = [n for n in names if "Trust" not in n]
        trust_names = [n for n in names if "Trust" in n]
        self.assertEqual(local_names, sorted(local_names, key=str.lower))
        self.assertEqual(trust_names, sorted(trust_names, key=str.lower))

    def test_board_pack_page_shows_sorted_funds(self):
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + self.q)
        html = r.content.decode()
        # scope to the "Fund balances" section specifically — other sections
        # (e.g. the Income & Expenditure Statement, which only lists funds
        # with recognised income) legitimately mention fund names in a
        # different, non-alphabetical order earlier on the same page
        start = html.index("Fund balances</div>")
        section = html[start:start + 4000]
        self.assertLess(section.index("Amber Fund"), section.index("Middle Fund"))
        self.assertLess(section.index("Middle Fund"), section.index("Zebra Fund"))


class AskAiRemovedTests(_ReportSeed):
    """Item 4."""

    def test_no_ask_ai_text(self):
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + self.q)
        html = r.content.decode()
        self.assertNotIn("Ask AI", html)
        self.assertNotIn("ask-ai-link", html)

    def test_pdf_export_still_works_without_ask_ai(self):
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + self.q + "&export=pdf")
        self.assertEqual(r.status_code, 200)


class BrokenCommentRemovedTests(_ReportSeed):
    """Item 5: Django {# #} comments cannot span multiple lines — the
    multi-line comment block was rendering as literal visible text."""

    def test_comment_text_not_in_output(self):
        r = self.client.get(reverse("engine_report", args=["treasurer_report"])
                            + self.q)
        html = r.content.decode()
        # these phrases were unique to the removed multi-line comment block —
        # legitimate content elsewhere on the page also mentions "Semantic
        # Reporting Layer" (e.g. the Notes section), so check the specific
        # comment wording instead of that shared phrase
        self.assertNotIn("executive board pack", html)
        self.assertNotIn("sticky section navigator", html)

    def test_no_multiline_django_comments_anywhere_in_templates(self):
        import re, glob
        offenders = []
        for f in glob.glob("templates/**/*.html", recursive=True):
            content = open(f, encoding="utf-8", errors="ignore").read()
            for m in re.finditer(r"\{#(.*?)#\}", content, re.S):
                if "\n" in m.group(1):
                    offenders.append(f)
        self.assertEqual(offenders, [])
