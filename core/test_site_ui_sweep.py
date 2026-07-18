"""Site-wide statement-design sweep (v2.94) — the contract, pinned.

Two halves:

1. **The global upgrade.** `.page-head` and `.ws-head` were restyled at the CSS
   level to join the statement family (forest-over-brass rule, spacing rhythm),
   so all ~246 templates using them upgraded at once. Crucially the PRINT rule
   changed: those headers used to be `display:none` in print — every page printed
   titleless, a bug reported and refixed page-by-page several times. Now they
   PRINT as a document head, with only the buttons/forms inside them hidden.
   These tests pin the print rule so a future stylesheet edit can't quietly
   bring the titleless-print bug back.

2. **The key pages.** The most-used screens carry the full masthead (eyebrow ·
   church name, title, description, rule) or at minimum the eyebrow. A render
   test asserts each key page loads and shows the statement marks.
"""
import pathlib
import re

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

CSS = pathlib.Path(__file__).resolve().parent.parent / "static" / "css" / "app.css"

KEY_PAGES = [
    "/members/", "/statements/", "/reconciliations/", "/petty-cash/",
    "/benevolent/", "/benevolent/cases/", "/benevolent/registry/",
    "/envelopes/", "/pledges/", "/payments/", "/transfers/",
    "/departments/", "/reports/budget-board/".replace("-board/", "/") and "/budget/",
    "/ledger/trial-balance/", "/ledger/general-ledger/",
]


class UiSweepCssContractTests(TestCase):
    def setUp(self):
        self.css = CSS.read_text()

    def test_headers_join_the_statement_family(self):
        # the double rule (forest over brass) under both header classes — the
        # SCREEN rules (each opens with content:""), not the print overrides
        for sel in (".page-head::after{content", ".ws-head::after{content"):
            self.assertIn(sel, self.css)
            block = self.css[self.css.index(sel):self.css.index(sel) + 300]
            self.assertIn("var(--forest)", block)
            self.assertIn("var(--brass)", block)

    def test_headers_print_as_a_document_head_not_hidden(self):
        """The recurring bug, pinned: page headers must NOT be display:none in
        print. They print as a document head; only actions inside them hide."""
        m = re.search(r"@media print(.*)", self.css, re.S)
        self.assertIsNotNone(m, "no print stylesheet found")
        printed = m.group(1)
        self.assertNotRegex(
            printed, r"\.page-head\s*,\s*\.ws-head\s*\{\s*display\s*:\s*none",
            "page headers are hidden in print again — every page would print "
            "titleless (the bug this sweep fixed globally)")
        # and the actions inside them DO hide
        self.assertRegex(printed, r"\.page-head \.btn.*display\s*:\s*none",
                         "buttons inside printed headers should not print")

    def test_no_dead_legacy_header_classes(self):
        self.assertNotIn("--legacy", self.css)


class KeyPageMastheadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from core.roles import TREASURER
        cls.user = User.objects.create_user("ui_sweep_t", password="x")
        cls.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])

    def setUp(self):
        self.c = Client()
        self.c.force_login(self.user)

    def test_key_pages_render_with_statement_marks(self):
        problems = []
        for page in KEY_PAGES:
            r = self.c.get(page)
            if r.status_code != 200:
                problems.append((page, f"HTTP {r.status_code}"))
                continue
            html = r.content.decode()
            # every key page carries the eyebrow (full masthead pages also carry
            # rpt-mast + rule; eyebrow is the common denominator)
            if 'class="eyebrow"' not in html:
                problems.append((page, "no eyebrow"))
        self.assertEqual(problems, [],
                         f"key pages missing the statement design: {problems}")

    def test_full_masthead_pages(self):
        """The hand-converted pages carry the complete masthead."""
        for page in ("/members/", "/statements/", "/benevolent/",
                     "/departments/", "/reconciliations/"):
            html = self.c.get(page).content.decode()
            self.assertIn("rpt-mast", html, f"{page} lost its masthead")
            self.assertIn('class="rule"', html, f"{page} lost its rule")
