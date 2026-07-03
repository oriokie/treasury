"""Treasurer dashboard chart rework (item 9): the Latest Sabbath date now uses
the theme's display font instead of a hardcoded one; the combined receipts-vs-
expenses-by-month chart moved to the Executive overview showing the full year
(not just the dashboard's selected period); its old spot on the dashboard is
now a local-vs-trust doughnut for the selected month."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction


def _tr():
    u = User.objects.create_user("tr_dash9", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class SabbathFontTests(TestCase):
    def test_sabbath_date_uses_theme_display_font(self):
        css = open("static/css/app.css").read()
        self.assertIn('.ss-date{font-family:var(--font-display', css)

    def test_sabbath_font_not_hardcoded(self):
        css = open("static/css/app.css").read()
        idx = css.index(".ss-date{")
        rule = css[idx:idx + 80]
        self.assertNotIn('font-family:Fraunces,Georgia,serif;', rule)


class DashboardChartSwapTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="D9Local", fund_type="LOCAL",
            category="MINISTRY")
        self.t = Department.objects.create(name="D9Trust", fund_type="TRUST",
            category="OFFERING")
        Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal("15000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.d)
        Transaction.objects.create(date=dt.date(2026, 6, 11), amount=Decimal("9000"),
            direction="CREDIT", confirmed=True, channel="BANK",
            allocation_status="MANUAL", department=self.t)
        self.c = Client(); self.c.force_login(self.tr)

    def test_dashboard_no_longer_has_monthly_receipts_expenses_chart(self):
        b = self.c.get("/?start=2026-06-01&end=2026-06-30").content.decode()
        self.assertNotIn("monthlyChart", b)
        self.assertNotIn("Receipts vs expenses by month", b)

    def test_dashboard_has_local_trust_pie(self):
        b = self.c.get("/?start=2026-06-01&end=2026-06-30").content.decode()
        self.assertIn("localTrustChart", b)
        self.assertIn("Local vs trust", b)

    def test_local_trust_json_values_correct(self):
        from core.views import DashboardView
        from django.test import RequestFactory
        rf = RequestFactory()
        req = rf.get("/?start=2026-06-01&end=2026-06-30")
        req.user = self.tr
        view = DashboardView()
        view.request = req
        resp = view.get(req)
        ctx = resp.context_data
        import json
        data = json.loads(ctx["local_trust_json"])
        self.assertEqual(data["local"], 15000.0)
        self.assertEqual(data["trust"], 9000.0)

    def test_executive_overview_has_full_year_chart(self):
        b = self.c.get("/executive/").content.decode()
        self.assertIn("receiptsVsExpensesYear", b)
        self.assertIn("Receipts vs expenses by month (this year)", b)

    def test_executive_chart_data_is_full_year(self):
        from core.services import dashboard
        data = dashboard.charts()
        self.assertIn("receipts_vs_expenses_year", data)
        rve = data["receipts_vs_expenses_year"]
        self.assertIn("receipts", rve)
        self.assertIn("expenses", rve)
        self.assertEqual(len(rve["labels"]), dt.date.today().month)
