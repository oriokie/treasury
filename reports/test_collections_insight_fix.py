"""Bug found incidentally while running the cashbook regression suite during
a related review: _section_insights()'s "collections" year-over-year
narrative used yearly[-1]["total"], but yearly_trend() returns entries keyed
"collection" (singular) — a KeyError, silently caught by the surrounding
try/except (an intentional "an optional narrative must never break the
report" safeguard), meaning this specific insight paragraph on the Monthly
Treasurer's Report never actually generated, on any report, ever."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User, Group
from django.test import RequestFactory
from departments.models import Department
from giving.models import Transaction
from reports.views import MonthlyTreasurerReportView


def _tr():
    u = User.objects.create_user("tr_insight_fix", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class CollectionsInsightTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="InsightFixFund", fund_type="LOCAL",
            category="MINISTRY")
        Transaction.objects.create(date=dt.date(2026, 6, 10), amount=Decimal("5000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.d)
        Transaction.objects.create(date=dt.date(2025, 6, 10), amount=Decimal("4000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.d)

    def test_collections_insight_generates_without_error(self):
        rf = RequestFactory()
        req = rf.get("/reports/board/?as_of=2026-06")
        req.user = self.tr
        view = MonthlyTreasurerReportView(); view.request = req
        ctx = view.get_context_data()
        self.assertIn("collections", ctx.get("insights") or {})

    def test_collections_insight_text_is_meaningful(self):
        rf = RequestFactory()
        req = rf.get("/reports/board/?as_of=2026-06")
        req.user = self.tr
        view = MonthlyTreasurerReportView(); view.request = req
        ctx = view.get_context_data()
        text = ctx["insights"]["collections"]
        self.assertIn("Year-to-date collections", text)
        self.assertNotIn("KeyError", text)

    def test_yearly_trend_keys_match_what_insights_expects(self):
        from reports.services.treasurer import yearly_trend
        rows = yearly_trend(dt.date(2026, 6, 30))
        for row in rows:
            self.assertIn("collection", row)
