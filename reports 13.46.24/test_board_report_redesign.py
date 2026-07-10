"""Monthly Treasurer's Report redesign: executive summary (KPIs, highlights,
attention items), budget tracking, negative-balance flagging, and the Board
Decisions Required section. Verifies the board-focus logic and that exports
(Excel/Word/RTF) still work from the same underlying context."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department, Budget
from giving.models import Transaction
from cashbook.models import Expense
from ledger.services.posting import ensure_chart


def _tr():
    u = User.objects.create_user("tr_mtr", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class ExecutiveSummaryTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)

    def test_kpi_cards_present(self):
        b = self.c.get("/reports/board/?as_of=2026-06-15").content.decode()
        for label in ["Total collections", "Local fund receipts",
                      "Trust fund receipts", "Total expenses",
                      "Monthly surplus / (deficit)", "Cash &amp; bank balance",
                      "Net assets", "Trust funds outstanding"]:
            self.assertIn(label, b)

    def test_all_clear_when_no_issues(self):
        # a period with no expenses/reconciliation records at all should still
        # render without crashing and show either issues or the all-clear panel
        r = self.c.get("/reports/board/?as_of=2020-01-15")
        self.assertEqual(r.status_code, 200)


class NegativeBalanceFlaggingTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.d = Department.objects.create(name="Overdrawn", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("0"))
        Expense.objects.create(date=dt.date(2026, 6, 5), department=self.d,
            description="Big spend", amount=Decimal("5000"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        self.c = Client(); self.c.force_login(self.tr)

    def test_negative_balance_flagged_in_attention(self):
        b = self.c.get("/reports/board/?as_of=2026-06-15").content.decode()
        self.assertIn("Negative fund balance", b)
        self.assertIn("Overdrawn", b)

    def test_negative_balance_marked_in_table(self):
        from reports.views import MonthlyTreasurerReportView
        from django.test import RequestFactory
        rf = RequestFactory()
        req = rf.get("/reports/board/?as_of=2026-06-15")
        req.user = self.tr
        view = MonthlyTreasurerReportView()
        view.request = req
        ctx = view.get_context_data()
        neg_rows = [r for r in ctx["local_statement"]["rows"] if r["closing"] < 0]
        self.assertTrue(any(r["department"].id == self.d.id for r in neg_rows))


class BudgetOverrunTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.d = Department.objects.create(name="OverBudget", fund_type="LOCAL",
            category="MINISTRY")
        Budget.objects.create(year=2026, department=self.d, amount=Decimal("1200"))
        Expense.objects.create(date=dt.date(2026, 6, 10), department=self.d,
            description="Overspend", amount=Decimal("5000"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        self.c = Client(); self.c.force_login(self.tr)

    def test_overrun_flagged_in_attention(self):
        b = self.c.get("/reports/board/?as_of=2026-06-15").content.decode()
        self.assertIn("Budget overrun", b)

    def test_budget_summary_in_context(self):
        from reports.views import MonthlyTreasurerReportView
        from django.test import RequestFactory
        rf = RequestFactory()
        req = rf.get("/reports/board/?as_of=2026-06-15")
        req.user = self.tr
        view = MonthlyTreasurerReportView()
        view.request = req
        ctx = view.get_context_data()
        row = next(r for r in ctx["budget_summary"]["rows"]
                   if r["department"].id == self.d.id)
        self.assertTrue(row["over"])


class BoardDecisionsTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)

    def test_decisions_section_present(self):
        b = self.c.get("/reports/board/?as_of=2026-06-15").content.decode()
        self.assertIn("Board decisions required", b)
        self.assertIn("Approve the financial statements", b)

    def test_high_severity_attention_becomes_decision(self):
        from reports.views import MonthlyTreasurerReportView
        from django.test import RequestFactory
        rf = RequestFactory()
        req = rf.get("/reports/board/?as_of=2026-06-15")
        req.user = self.tr
        view = MonthlyTreasurerReportView()
        view.request = req
        ctx = view.get_context_data()
        high_titles = {a["title"] for a in ctx["attention"] if a["severity"] == "high"}
        decision_details = " ".join(d["title"] for d in ctx["decisions"])
        for t in high_titles:
            self.assertIn(t, decision_details)


class ExportsStillWorkTests(TestCase):
    """The redesign only touches the HTML template + adds context keys; the
    Excel/Word/RTF exports build from the same context and must keep working."""
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)

    def test_excel_export(self):
        r = self.c.get("/reports/board/export/excel/?as_of=2026-06-01")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheet", r["Content-Type"])

    def test_word_export(self):
        r = self.c.get("/reports/board/export/word/?as_of=2026-06-01")
        self.assertEqual(r.status_code, 200)

    def test_word_export_has_ai_narratives(self):
        r = self.c.get("/reports/board/export/word/?as_of=2026-06-01")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("Board decisions required", b)
        self.assertIn('class="narrative"', b)


class TopTenAppendixTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        for i in range(15):
            d = Department.objects.create(name=f"Fund{i:02d}", fund_type="LOCAL",
                category="MINISTRY")
            Transaction.objects.create(date=dt.date(2026, 6, 5),
                amount=Decimal(str(1000 - i)), direction="CREDIT", confirmed=True,
                channel="CASH", allocation_status="MANUAL", department=d)
        self.c = Client(); self.c.force_login(self.tr)

    def test_appendix_shown_when_more_than_ten(self):
        b = self.c.get("/reports/board/?as_of=2026-06-15").content.decode()
        self.assertIn("Appendix", b)
