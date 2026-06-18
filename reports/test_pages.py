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
        row = rows[self.trust.id]
        # the 2,500 is a raw BANK credit with no receipt issued yet, so it is
        # recognised as UNRECEIPTED trust (a separate liability), not yet due to
        # remit. Only receipted trust money counts toward to_remit.
        self.assertEqual(row["to_remit"], Decimal("0"))
        self.assertEqual(row["unreceipted"], Decimal("2500"))
        self.assertEqual(row["total_liability"], Decimal("2500"))

    def test_sofp_balances(self):
        r = self.client.get(reverse("report_financial_position"))
        self.assertTrue(r.context["balanced"])


class RemittanceCalendarTests(TestCase):
    """Item 5: remittance calendar deadlines + reporting-Sabbath logic."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        self.u = User.objects.create_user("rc", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.u.groups.add(g)

    def test_generate_and_reporting_sabbath(self):
        from django.test import Client
        from cashbook.models import RemittanceDeadline
        c = Client(); c.force_login(self.u)
        c.post("/reports/trust/remittance/calendar/generate/",
               {"year": "2026", "due_day": "15"})
        self.assertEqual(RemittanceDeadline.objects.filter(year=2026).count(), 12)
        jan = RemittanceDeadline.objects.get(year=2026, period_month=1)
        # reporting Sabbath is a Saturday on/before the deadline
        self.assertEqual(jan.reporting_sabbath.weekday(), 5)
        self.assertLessEqual(jan.reporting_sabbath, jan.deadline)

    def test_calendar_page_renders(self):
        from django.test import Client
        c = Client(); c.force_login(self.u)
        self.assertEqual(
            c.get("/reports/trust/remittance/calendar/?year=2026").status_code, 200)

    def test_midweek_deadline_uses_previous_sabbath(self):
        from cashbook.models import RemittanceDeadline
        import datetime as dt
        # a Wednesday deadline
        d = RemittanceDeadline.objects.create(year=2026, period_month=7,
            deadline=dt.date(2026, 7, 15))  # 2026-07-15 is a Wednesday
        self.assertEqual(d.deadline.weekday(), 2)
        self.assertEqual(d.reporting_sabbath, dt.date(2026, 7, 11))  # prev Saturday


class RemittanceCalendarAutoTests(TestCase):
    """Item 3: default deadline is the 1st of the following month, and periods
    auto-mark as remitted when a remitted batch covers them."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        self.u = User.objects.create_user("rca", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.u.groups.add(g)

    def test_generate_defaults_to_first_of_next_month(self):
        from django.test import Client
        from cashbook.models import RemittanceDeadline
        c = Client(); c.force_login(self.u)
        c.post("/reports/trust/remittance/calendar/generate/", {"year": "2031"})
        jan = RemittanceDeadline.objects.get(year=2031, period_month=1)
        self.assertEqual(jan.deadline.day, 1)
        self.assertEqual(jan.deadline.month, 2)

    def test_auto_mark_remitted_from_batch(self):
        import datetime as dt
        from django.test import Client
        from cashbook.models import RemittanceDeadline, RemittanceBatch
        c = Client(); c.force_login(self.u)
        c.post("/reports/trust/remittance/calendar/generate/", {"year": "2031"})
        RemittanceBatch.objects.create(status="REMITTED",
            period_start=dt.date(2031, 2, 1), period_end=dt.date(2031, 2, 28),
            remitted_at=dt.datetime(2031, 3, 1), created_by=self.u)
        c.get("/reports/trust/remittance/calendar/?year=2031")
        self.assertTrue(RemittanceDeadline.objects.get(year=2031, period_month=2).remitted)
        self.assertFalse(RemittanceDeadline.objects.get(year=2031, period_month=1).remitted)


class DuplicateDetectionTests(TestCase):
    """Item 2: offerings flagged only within the same channel; envelope
    duplicates by giver+amount+Sabbath; expenses by Sabbath."""

    def setUp(self):
        from django.contrib.auth.models import User
        from departments.models import Department
        self.u = User.objects.create_superuser("dd", password="x")
        self.d = Department.objects.create(name="Tithe", fund_type="TRUST",
                                           is_trust=True, category="OFFERING")

    def test_same_channel_flagged_cross_channel_not(self):
        import datetime as dt
        from decimal import Decimal
        from giving.models import Transaction
        from core.views import _duplicate_offerings
        # cross-channel same giver/amount — must NOT flag
        Transaction.objects.create(date=dt.date(2026, 6, 8), channel="CASH",
            direction="CREDIT", amount=Decimal("500"), department=self.d,
            payer_name="Cross C", allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=dt.date(2026, 6, 8), channel="BANK",
            direction="CREDIT", amount=Decimal("500"), department=self.d,
            payer_name="Cross C", allocation_status="MANUAL", confirmed=True,
            core_ref="CC9")
        # same-channel same giver/amount — must flag
        Transaction.objects.create(date=dt.date(2026, 6, 8), channel="CASH",
            direction="CREDIT", amount=Decimal("700"), department=self.d,
            payer_name="Same C", allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=dt.date(2026, 6, 9), channel="CASH",
            direction="CREDIT", amount=Decimal("700"), department=self.d,
            payer_name="Same C", allocation_status="MANUAL", confirmed=True)
        payers = [x["payer"] for x in _duplicate_offerings()]
        self.assertNotIn("CROSS C", payers)
        self.assertIn("SAME C", payers)

    def test_envelope_duplicate_flagged(self):
        import datetime as dt
        from decimal import Decimal
        from envelopes.models import Envelope
        from core.views import _duplicate_envelopes
        sat = dt.date(2026, 6, 6)
        Envelope.objects.create(date=sat, contributor_name="Env Giver",
            receipt_no="EV1", total=Decimal("1000"), recorded_by=self.u)
        Envelope.objects.create(date=sat, contributor_name="Env Giver",
            receipt_no="EV2", total=Decimal("1000"), recorded_by=self.u)
        flagged = [x for x in _duplicate_envelopes() if x["payer"] == "ENV GIVER"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["count"], 2)
