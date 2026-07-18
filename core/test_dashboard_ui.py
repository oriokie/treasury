"""Dashboard UI (v2.86) — the statement masthead and section rhythm on both
dashboards, and the executive palette discipline.

Pins the contract, not pixels: both dashboards open with the `.rpt-mast`
masthead (which also restores a printed header — `.ws-head`/`.page-head` are
hidden in print CSS), section heads use the shared `.sec-h` brass-hairline
vocabulary, the main dashboard shows "needs attention" once (the pill row only
appears where the richer attention grid is absent), and the executive charts
read their colours from the app's CSS tokens rather than hardcoding — in
particular the off-palette terracotta is gone.
"""
from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER


class DashboardUiTests(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_dash", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)

    def test_dashboard_masthead(self):
        h = self.client.get("/").content.decode()
        self.assertIn("rpt-mast", h)
        self.assertIn('class="eyebrow"', h)
        self.assertIn('class="rule"', h)

    def test_dashboard_section_rhythm(self):
        h = self.client.get("/").content.decode()
        self.assertIn('class="sec-h"', h)

    def test_dashboard_date_filter_labeled(self):
        h = self.client.get("/").content.decode()
        self.assertIn('for="d_start"', h)
        self.assertIn('for="d_end"', h)

    def test_attention_not_duplicated(self):
        h = self.client.get("/").content.decode()
        # where the attention grid renders, the brass pill row must not also
        if "attention-card" in h:
            self.assertNotIn("alert-brass", h)

    def test_executive_masthead_and_board_print(self):
        h = self.client.get("/executive/").content.decode()
        self.assertIn("rpt-mast", h)
        self.assertIn("Executive ·", h)
        self.assertIn("Board copy", h)

    def test_executive_sections_and_tokens(self):
        h = self.client.get("/executive/").content.decode()
        self.assertEqual(h.count('class="sec-h"'), 5)
        # charts read tokens, no off-palette terracotta
        self.assertIn("getPropertyValue('--danger')", h)
        self.assertNotIn("#c0532b", h)

    def test_executive_card_typography(self):
        h = self.client.get("/executive/").content.decode()
        self.assertIn("IBM Plex Mono", h)
        self.assertIn("tabular-nums", h)
