"""Render smoke tests for the cashbook and core pages a treasurer reaches with no
URL arguments. Guards against template/context regressions (500s) and lifts view
coverage on the GET paths."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from cashbook.models import Expense


def _user(name, role):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


CASHBOOK_PAGES = [
    "expense_list", "expense_create", "transfer_list", "transfer_create",
    "recurring_list", "recurring_create", "accruals", "expense_categories",
    "advance_list", "advance_new", "petty_cash",
]
CORE_PAGES = ["settings", "notifications", "controls", "executive"]


class CashbookCorePagesRenderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.treasurer = _user("pg_tr", TREASURER)
        cls.fund = Department.objects.create(name="LCB", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("1000"))
        Transaction.objects.create(date=dt.date.today(), channel="BANK",
            direction="CREDIT", amount=Decimal("500"), department=cls.fund,
            allocation_status="AUTO", confirmed=True, core_ref="PG1")
        cls.expense = Expense.objects.create(date=dt.date.today(), department=cls.fund,
            description="Tea", amount=Decimal("100"), category="REFRESHMENTS",
            status="PENDING", recorded_by=cls.treasurer)

    def setUp(self):
        self.client.force_login(self.treasurer)

    def test_cashbook_pages_render(self):
        failures = []
        for name in CASHBOOK_PAGES:
            try:
                url = reverse(name)
            except Exception:
                continue
            code = self.client.get(url).status_code
            if code != 200:
                failures.append((name, code))
        self.assertEqual(failures, [], f"cashbook pages not 200: {failures}")

    def test_core_pages_render(self):
        failures = []
        for name in CORE_PAGES:
            try:
                url = reverse(name)
            except Exception:
                continue
            code = self.client.get(url).status_code
            if code != 200:
                failures.append((name, code))
        self.assertEqual(failures, [], f"core pages not 200: {failures}")

    def test_expense_detail_and_edit_render(self):
        self.assertEqual(self.client.get(
            reverse("expense_detail", args=[self.expense.pk])).status_code, 200)
        self.assertEqual(self.client.get(
            reverse("expense_edit", args=[self.expense.pk])).status_code, 200)

    def test_dashboard_renders_with_data(self):
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)


class ExpenseSearchAndRecategorizeTests(TestCase):
    """Items 4 & 5: expense search filter, and category-only bulk re-import."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        from departments.models import Department
        from cashbook.models import Expense
        import datetime as dt
        from decimal import Decimal
        self.u = User.objects.create_user("rc", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.u.groups.add(g)
        self.d = Department.objects.create(name="LCB", fund_type="LOCAL",
                                           category="OFFERING")
        self.e = Expense.objects.create(date=dt.date(2026, 6, 1), department=self.d,
            description="Generator fuel", amount=Decimal("500"), category="MATERIALS",
            status="PAID", recorded_by=self.u, claimant="John")

    def test_search_matches_description(self):
        from django.test import Client
        c = Client(); c.force_login(self.u)
        r = c.get("/expenses/?q=generator")
        self.assertContains(r, "Generator fuel")
        r2 = c.get("/expenses/?q=nothinghere")
        self.assertNotContains(r2, "Generator fuel")

    def test_recategorize_updates_only_category(self):
        import io, openpyxl
        from django.test import Client
        from django.core.files.uploadedfile import SimpleUploadedFile
        c = Client(); c.force_login(self.u)
        r = c.get("/expenses/recategorize/?download=1")
        self.assertEqual(r.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        ws = wb["Expenses"]
        hdr = [cell.value for cell in ws[1]]
        idc = hdr.index("ID"); newc = hdr.index("New category (edit this)")
        for row in ws.iter_rows(min_row=2):
            if row[idc].value == self.e.id:
                row[newc].value = "Transport"
                row[2].value = "SHOULD_BE_IGNORED"
        buf = io.BytesIO(); wb.save(buf)
        up = SimpleUploadedFile("x.xlsx", buf.getvalue())
        c.post("/expenses/recategorize/", {"file": up})
        self.e.refresh_from_db()
        self.assertEqual(self.e.category, "TRANSPORT")
        self.assertEqual(self.e.description, "Generator fuel")  # untouched
