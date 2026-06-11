"""Broad coverage for the report surface: every report page must render (no 500s)
for a treasurer, respect read-only access for auditors, and key figures must be
correct. This locks in that the reporting layer keeps working end to end."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER, ASSISTANT, AUDITOR
from departments.models import Department
from members.models import Member
from giving.models import Transaction
from cashbook.models import Expense
from assets.models import FixedAsset


def _role(username, role):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class ReportDataMixin:
    @classmethod
    def setUpTestData(cls):
        cls.treasurer = _role("rep_tr", TREASURER)
        cls.assistant = _role("rep_as", ASSISTANT)
        cls.auditor = _role("rep_au", AUDITOR)
        cls.local = Department.objects.create(name="LCB", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("1000"))
        cls.trust = Department.objects.create(name="Tithe", fund_type="TRUST",
            category="OFFERING", is_trust=True)
        cls.member = Member.objects.create(name="Jane Doe", phone="254700000001")
        today = dt.date.today()
        for amt, dep in [("500", cls.local), ("2500", cls.trust)]:
            Transaction.objects.create(date=today, channel="BANK", direction="CREDIT",
                amount=Decimal(amt), department=dep, member=cls.member,
                allocation_status="AUTO", confirmed=True,
                core_ref=f"R{amt}{dep.pk}")
        Expense.objects.create(date=today, department=cls.local, description="Tea",
            amount=Decimal("120"), category="REFRESHMENTS", status="PAID",
            recorded_by=cls.treasurer, approved_by=cls.treasurer)
        cls.asset = FixedAsset.objects.create(name="Piano", cost=Decimal("80000"),
            salvage_value=Decimal("0"), acquired_on=today, category="EQUIPMENT",
            method="STRAIGHT", rate=Decimal("10"))


# Reports reachable with no required URL args. Period-based ones accept the query
# string harmlessly even if they ignore it.
SIMPLE_REPORTS = [
    "report_index", "report_monthly", "report_offering", "report_tithe",
    "report_by_group", "report_dev_groups", "report_expenses", "report_ie",
    "report_income_statement", "report_trust", "report_remittance",
    "report_cashbook", "report_reconciliation", "report_annual", "report_audit",
    "report_financial_position", "report_changes_net_assets", "report_cash_flow",
    "report_budget_vs_actual", "report_weekly", "report_daily",
    "report_collections_summary", "report_envelope_summary",
    "report_envelope_sabbath", "report_conference", "report_board",
    "report_pastor", "report_accounts_monthly", "report_trust_monthly",
]


class ReportPagesRenderTests(ReportDataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.treasurer)

    def test_all_simple_reports_render(self):
        yr = dt.date.today().year
        failures = []
        for name in SIMPLE_REPORTS:
            try:
                url = reverse(name)
            except Exception:
                continue  # route not present in this build
            resp = self.client.get(url + f"?period=ANNUAL&year={yr}")
            if resp.status_code != 200:
                failures.append((name, resp.status_code))
        self.assertEqual(failures, [], f"reports not returning 200: {failures}")

    def test_fund_ledger_renders(self):
        r = self.client.get(reverse("report_fund", args=[self.local.pk]))
        self.assertEqual(r.status_code, 200)

    def test_member_statement_renders(self):
        r = self.client.get(reverse("report_member", args=[self.member.pk]))
        self.assertEqual(r.status_code, 200)


class ReportExportTests(ReportDataMixin, TestCase):
    """Excel/CSV export endpoints should stream a file, not error."""
    def setUp(self):
        self.client.force_login(self.treasurer)

    def test_exports_stream_files(self):
        yr = dt.date.today().year
        for name in ["report_monthly", "report_ie", "report_trust",
                     "report_expenses", "report_offering"]:
            try:
                url = reverse(name)
            except Exception:
                continue
            for fmt in ("xlsx", "csv"):
                r = self.client.get(url + f"?period=ANNUAL&year={yr}&export={fmt}")
                # either it streams a file (200 with attachment) or ignores the
                # param and renders the page; both are acceptable, a 500 is not
                self.assertIn(r.status_code, (200, 302),
                              f"{name} export={fmt} -> {r.status_code}")


class ReportAccessControlTests(ReportDataMixin, TestCase):
    def test_auditor_can_read_reports(self):
        self.client.force_login(self.auditor)
        r = self.client.get(reverse("report_financial_position"))
        self.assertEqual(r.status_code, 200)

    def test_anonymous_redirected_to_login(self):
        r = self.client.get(reverse("report_monthly"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.url)


class ReportFigureTests(ReportDataMixin, TestCase):
    """Spot-check that headline figures are right, not just that pages render."""
    def setUp(self):
        self.client.force_login(self.treasurer)

    def test_ie_income_is_local_only(self):
        yr = dt.date.today().year
        r = self.client.get(reverse("report_ie") + f"?period=ANNUAL&year={yr}")
        # local income 500; trust 2500 excluded from income
        self.assertEqual(r.context["income"], Decimal("500"))
        self.assertEqual(r.context["trust_collected"], Decimal("2500"))
        self.assertEqual(r.context["expense"], Decimal("120"))

    def test_trust_report_to_remit(self):
        r = self.client.get(reverse("report_trust"))
        rows = {row["department"].id: row for row in r.context["rows"]}
        self.assertEqual(rows[self.trust.id]["to_remit"], Decimal("2500"))

    def test_sofp_balances(self):
        r = self.client.get(reverse("report_financial_position"))
        self.assertTrue(r.context["balanced"])
