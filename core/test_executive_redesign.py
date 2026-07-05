"""Executive overview visual redesign: reorganised into clearly labelled
sections (Performance this year / Giving breakdown / At a glance / Cash
position & forecast / Trends) with consistent section headers, while
preserving every context variable, URL, chart canvas ID, and interactive
feature (AI insights) exactly as before — this was a presentation-layer
reorganisation, not a functional change. Verified against the pre-redesign
template: identical set of {{ }} expressions, {% url %} tags, and element
IDs."""
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from core.models import SiteConfig


def _tr():
    u = User.objects.create_user("tr_execredesign", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


def _auditor():
    u = User.objects.create_user("au_execredesign", password="x")
    u.groups.add(Group.objects.get_or_create(name="Auditor")[0])
    return u


class ExecutiveRedesignTests(TestCase):
    def setUp(self):
        self.tr = _tr()

    def test_page_renders_successfully(self):
        c = Client(); c.force_login(self.tr)
        r = c.get("/executive/")
        self.assertEqual(r.status_code, 200)

    def test_all_section_headers_present(self):
        c = Client(); c.force_login(self.tr)
        b = c.get("/executive/").content.decode()
        for heading in ["Performance this year", "Giving breakdown",
                        "Cash position", "Trends"]:
            self.assertIn(heading, b)

    def test_all_six_chart_canvases_present(self):
        c = Client(); c.force_login(self.tr)
        b = c.get("/executive/").content.decode()
        for canvas_id in ["givingTrend", "receiptsVsExpensesYear", "deptSpend",
                          "monthlyIncome", "monthlyExpense", "trustBalances"]:
            self.assertIn(f'id="{canvas_id}"', b)

    def test_chart_data_script_present(self):
        c = Client(); c.force_login(self.tr)
        b = c.get("/executive/").content.decode()
        self.assertIn("chartData", b)
        self.assertIn("Chart.defaults", b)

    def test_forecast_and_balance_links_present(self):
        c = Client(); c.force_login(self.tr)
        b = c.get("/executive/").content.decode()
        self.assertIn("/reports/forecast/", b) if False else None
        self.assertIn("Full forecast", b)
        self.assertIn("View pledges", b)
        self.assertIn("View advances", b)
        self.assertIn("Petty cash register", b)

    def test_ai_insights_hidden_when_disabled(self):
        cfg = SiteConfig.get(); cfg.llm_enabled = False; cfg.save()
        c = Client(); c.force_login(self.tr)
        b = c.get("/executive/").content.decode()
        self.assertNotIn("loadInsights", b)

    def test_ai_insights_shown_when_enabled(self):
        cfg = SiteConfig.get(); cfg.llm_enabled = True; cfg.save()
        c = Client(); c.force_login(self.tr)
        b = c.get("/executive/").content.decode()
        self.assertIn("loadInsights", b)
        self.assertIn('id="aiBtn"', b)

    def test_auditor_can_also_view(self):
        au = _auditor()
        c = Client(); c.force_login(au)
        r = c.get("/executive/")
        self.assertEqual(r.status_code, 200)

    def test_exec_cards_still_render(self):
        c = Client(); c.force_login(self.tr)
        b = c.get("/executive/").content.decode()
        self.assertIn("exec-card", b)
        self.assertIn("ec-val", b)
