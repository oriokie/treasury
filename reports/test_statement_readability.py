"""A statement's totals must look like totals.

The report engine marks subtotal rows and grand-total footers with `row-emph`,
and that class was defined nowhere. Every engine report therefore set its totals
exactly like the line items above them: a statement of financial position listed
bank, petty cash, staff advances, total assets, trust payable, loans, total
liabilities and net assets in one undifferentiated column of figures, leaving the
reader to already know the statement's shape in order to find the three numbers
it exists to give.

A missing CSS class is silent by construction — the page loads, the markup is
valid, and the only symptom is that a report quietly looks unfinished. The
existing `core.test_css_contract` guard did not catch this one because it only
fails on classes used in three or more templates, and `row-emph` is used in one:
the single template every engine report renders through. Reach, not repetition,
is what mattered here, and that is why this suite pins the class by name.

Emphasis alone was also not enough. A group heading, a subtotal and the bottom
line are three different things, and all three arrived as "emphasised". Sections
can now say which, and the tests below hold both halves: that the levels are
carried, and that adding them changed no figure.
"""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from core import roles
from core.reporting.engine import Section

CSS = (Path(settings.BASE_DIR) / "static" / "css" / "app.css").read_text(encoding="utf-8")


class StatementRowStylesAreDefinedTests(SimpleTestCase):
    """The classes the engine emits must exist in the stylesheet."""

    def test_the_emphasis_class_is_defined(self):
        self.assertIn(
            "row-emph", CSS,
            "`row-emph` marks every subtotal and grand total the report engine "
            "renders. Undefined, every total on every report is set like an "
            "ordinary line item.")

    def test_each_statement_level_is_defined(self):
        for level in ("row-heading", "row-subtotal", "row-grand"):
            with self.subTest(level=level):
                self.assertIn(level, CSS)

    def test_the_bottom_line_is_set_apart_from_a_subtotal(self):
        """Otherwise the levels are a distinction the reader cannot see."""
        grand = re.search(r"tr\.row-grand\s*>\s*td\{([^}]*)\}", CSS)
        self.assertIsNotNone(grand, "No rule for the grand-total row.")
        body = grand.group(1)
        self.assertIn("double", body,
                      "A closing figure is conventionally double-ruled.")

    def test_the_styles_survive_printing(self):
        """A treasurer's statement is read on paper as often as on screen."""
        printed = CSS.split("@media print")[-1]
        self.assertIn("row-grand", printed)


class KeyvalueLevelsTests(SimpleTestCase):
    """`Section.keyvalue` carries the level without breaking older callers."""

    def test_a_plain_pair_is_an_ordinary_row(self):
        section = Section.keyvalue("k", "T", [("Bank", 100)])
        self.assertFalse(section.rows[0].emphasis)
        self.assertEqual(section.rows[0].meta.get("level", ""), "")

    def test_a_bare_true_still_means_emphasis(self):
        """Every existing caller passes True; none of them may break."""
        section = Section.keyvalue("k", "T", [("Total", 100, True)])
        self.assertTrue(section.rows[0].emphasis)
        self.assertEqual(section.rows[0].meta["level"], "subtotal")

    def test_a_named_level_is_carried(self):
        section = Section.keyvalue("k", "T", [
            ("Assets", None, "heading"),
            ("Total assets", 100, "subtotal"),
            ("Net assets", 40, "grand")])
        self.assertEqual([r.meta.get("level") for r in section.rows],
                         ["heading", "subtotal", "grand"])

    def test_every_level_is_emphasised(self):
        for level in ("heading", "subtotal", "grand"):
            with self.subTest(level=level):
                section = Section.keyvalue("k", "T", [("x", 1, level)])
                self.assertTrue(section.rows[0].emphasis)


class FinancialPositionIsReadableTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("tess-sofp", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.client = Client()
        self.client.force_login(self.user)

    def _section(self):
        from core.reporting.context import ReportContext
        from reports.financial_statements import FinancialPositionSummarySection
        return FinancialPositionSummarySection().render(
            ReportContext(start=None, end=None), {})

    def _values(self):
        return {r.cells["label"]: r.cells["value"] for r in self._section().rows}

    # -- the numbers did not move --------------------------------------------

    def test_net_assets_is_still_assets_less_liabilities(self):
        """The statement was restyled, not recalculated."""
        values = self._values()
        self.assertEqual(
            values["Total assets"] - values["Total liabilities"],
            values["Net assets"])

    def test_every_figure_line_still_carries_a_figure(self):
        for row in self._section().rows:
            if row.meta.get("level") == "heading":
                continue
            with self.subTest(label=row.cells["label"]):
                self.assertIsNotNone(row.cells["value"])

    # -- and the reader can find them ----------------------------------------

    def test_assets_and_liabilities_are_grouped_under_headings(self):
        headings = [r.cells["label"] for r in self._section().rows
                    if r.meta.get("level") == "heading"]
        self.assertEqual(headings, ["Assets", "Liabilities"])

    def test_a_heading_carries_no_figure_of_its_own(self):
        for row in self._section().rows:
            if row.meta.get("level") == "heading":
                self.assertIsNone(row.cells["value"])

    def test_the_two_subtotals_are_marked_as_subtotals(self):
        subtotals = [r.cells["label"] for r in self._section().rows
                     if r.meta.get("level") == "subtotal"]
        self.assertEqual(subtotals, ["Total assets", "Total liabilities"])

    def test_net_assets_is_marked_as_the_closing_figure(self):
        grand = [r.cells["label"] for r in self._section().rows
                 if r.meta.get("level") == "grand"]
        self.assertEqual(grand, ["Net assets"],
                         "The figure the statement exists to give is not set "
                         "apart from the subtotals above it.")

    def test_the_statement_says_what_net_assets_means(self):
        self.assertIn("total assets less total liabilities", self._section().note)

    # -- through the page -----------------------------------------------------

    def test_the_page_emits_the_level_classes(self):
        body = self.client.get(
            reverse("engine_report", args=["financial_position_v2"])).content.decode()
        for cls in ("row-heading", "row-subtotal", "row-grand"):
            with self.subTest(cls=cls):
                self.assertIn(cls, body)

    def test_a_heading_row_prints_no_stray_dash(self):
        """A heading with no figure must not render as a zero or a dash."""
        body = self.client.get(
            reverse("engine_report", args=["financial_position_v2"])).content.decode()
        heading_row = re.search(r'<tr class="[^"]*row-heading[^"]*">(.*?)</tr>',
                                body, re.S)
        self.assertIsNotNone(heading_row)
        self.assertNotIn("—", heading_row.group(1))
        self.assertNotIn("0.00", heading_row.group(1))


class OtherReportsStillRenderTests(TestCase):
    """The engine change touches every report, so a spread of them is checked."""

    def setUp(self):
        self.user = User.objects.create_user("tess-eng", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_engine_reports_still_render(self):
        for key in ("financial_position_v2", "income_statement_v2",
                    "fund_balances_v2", "treasurer_report"):
            with self.subTest(report=key):
                response = self.client.get(reverse("engine_report", args=[key]))
                self.assertEqual(response.status_code, 200)

    def test_reports_still_export(self):
        for fmt in ("csv", "xlsx", "pdf"):
            with self.subTest(format=fmt):
                response = self.client.get(
                    reverse("engine_report", args=["financial_position_v2"]),
                    {"export": fmt})
                self.assertEqual(response.status_code, 200)
