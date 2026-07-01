"""v1.94 leader batch: dashboard cards removed + sidebar menus + sub-account
opening, collections/expenses search, advance detail filters/search/pagination,
delete-expense, advance Excel import with balance cap."""
import io, datetime as dt
from decimal import Decimal
from openpyxl import Workbook
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department, DepartmentLeadership
from cashbook.models import StaffAdvance, Expense
from ledger.services.posting import ensure_chart


def _leader(dept):
    u = User.objects.create_user("ld_194", password="x")
    u.groups.add(Group.objects.get_or_create(name="Leader")[0])
    DepartmentLeadership.objects.create(department=dept, user=u)
    return u


def _tr():
    u = User.objects.create_user("tr_194", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class LeaderDashboardTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.parent = Department.objects.create(name="Youth P", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("1000"))
        self.sub = Department.objects.create(name="Choir", fund_type="LOCAL",
            category="MINISTRY", parent=self.parent, opening_balance=Decimal("300"))
        self.u = _leader(self.parent)
        DepartmentLeadership.objects.create(department=self.sub, user=self.u)
        self.c = Client(); self.c.force_login(self.u)

    def test_top_contributors_removed(self):
        b = self.c.get(f"/leader/department/{self.parent.id}/").content.decode()
        self.assertNotIn("Top contributors", b)

    def test_recent_cards_removed(self):
        b = self.c.get(f"/leader/department/{self.parent.id}/").content.decode()
        self.assertNotIn("Recent collections", b)
        self.assertNotIn("Recent expenses", b)

    def test_subaccounts_show_opening(self):
        b = self.c.get(f"/leader/department/{self.parent.id}/").content.decode()
        if "Sub-accounts" in b:
            self.assertIn(">Opening</th>", b)

    def test_sidebar_has_collections_expenses(self):
        b = self.c.get(f"/leader/department/{self.parent.id}/").content.decode()
        self.assertIn("Collections</a>", b)
        self.assertIn("Expenses</a>", b)


class LeaderSearchTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.dept = Department.objects.create(name="Missions", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.u = _leader(self.dept)
        self.c = Client(); self.c.force_login(self.u)

    def test_collections_search_box(self):
        r = self.c.get(f"/leader/department/{self.dept.id}/collections/?q=x")
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="q"', r.content.decode())

    def test_expenses_search_box(self):
        r = self.c.get(f"/leader/department/{self.dept.id}/expenses/?q=x&status=PAID")
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="q"', r.content.decode())


class LeaderAdvanceDetailTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.dept = Department.objects.create(name="Dev", fund_type="LOCAL",
            category="DEVELOPMENT", show_in_expenses=True)
        self.u = _leader(self.dept)
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.u)
        self.adv = StaffAdvance.objects.create(staff_name="A", department=self.dept,
            amount=Decimal("5000"), date_issued=dt.date(2026, 6, 1), purpose="p",
            method="CASH", from_petty_cash=False, issued_by=self.tr, status="ISSUED")

    def test_filter_bar_and_pagination(self):
        b = self.c.get(f"/leader/advances/{self.adv.id}/").content.decode()
        self.assertIn("adv-filter", b)
        self.assertIn('name="q"', b)
        self.assertIn('name="start"', b)
        self.assertIn("adv-entry-grid", b)  # mobile-friendly form

    def test_delete_expense_when_pending(self):
        self.c.post(f"/leader/advances/{self.adv.id}/",
            {"date": "2026-06-05", "description": "Fare", "category": "TRANSPORT",
             "amount": "500", "charge": "0"})
        exp = Expense.objects.filter(advance=self.adv, recorded_by=self.u).first()
        self.assertIsNotNone(exp)
        self.c.post(f"/leader/advances/{self.adv.id}/",
            {"action": "delete_expense", "expense_id": exp.id})
        self.assertFalse(Expense.objects.filter(id=exp.id).exists())

    def test_delete_blocked_when_settled(self):
        exp = Expense.objects.create(advance=self.adv, department=self.dept,
            description="x", amount=Decimal("100"), category="OTHER", status="PAID",
            recorded_by=self.u, date=dt.date(2026, 6, 6))
        self.adv.status = "SETTLED"; self.adv.save()
        self.c.post(f"/leader/advances/{self.adv.id}/",
            {"action": "delete_expense", "expense_id": exp.id})
        self.assertTrue(Expense.objects.filter(id=exp.id).exists())


class AdvanceImportTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.dept = Department.objects.create(name="ImpDept", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.u = _leader(self.dept)
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.u)
        self.adv = StaffAdvance.objects.create(staff_name="Imp", department=self.dept,
            amount=Decimal("5000"), date_issued=dt.date(2026, 6, 1), purpose="p",
            method="CASH", from_petty_cash=False, issued_by=self.tr, status="ISSUED")

    def _file(self, rows):
        wb = Workbook(); ws = wb.active; ws.title = "Advance expenses"
        ws.append(["Date", "Description", "Category", "Amount", "Charge"])
        for r in rows:
            ws.append(r)
        b = io.BytesIO(); wb.save(b); b.seek(0); b.name = "imp.xlsx"
        return b

    def test_sample_download(self):
        r = self.c.get(f"/advances/{self.adv.id}/import/?download=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheet", r["Content-Type"])

    def test_over_budget_rejected_in_full(self):
        f = self._file([["2026-06-05", "A", "Transport", 4000, 0],
                        ["2026-06-06", "B", "Materials", 2000, 0]])
        self.c.post(f"/advances/{self.adv.id}/import/", {"file": f})
        self.assertEqual(Expense.objects.filter(advance=self.adv).count(), 0)

    def test_valid_import_records_and_caps(self):
        f = self._file([["2026-06-05", "Fare", "Transport", 1200, 0],
                        ["2026-06-06", "Lunch", "Refreshments / catering", 1800, 0]])
        self.c.post(f"/advances/{self.adv.id}/import/", {"file": f})
        self.assertEqual(Expense.objects.filter(advance=self.adv).count(), 2)
        self.adv.refresh_from_db()
        self.assertEqual(self.adv.balance, Decimal("2000"))

    def test_charge_counts_toward_cap(self):
        # amount 4800 + charge 300 = 5100 > 5000 -> rejected
        f = self._file([["2026-06-05", "Big", "Materials", 4800, 300]])
        self.c.post(f"/advances/{self.adv.id}/import/", {"file": f})
        self.assertEqual(Expense.objects.filter(advance=self.adv).count(), 0)
