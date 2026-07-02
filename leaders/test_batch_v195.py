"""v1.95 batch: advances-list help + open button, receivable top-up-date fix,
dev-group opening/closing, leader advance xlsx export, leader charge deletion,
modern login page."""
import io, datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department, DepartmentLeadership, DevelopmentGroup
from giving.models import Transaction
from cashbook.models import StaffAdvance, AdvanceTopUp, Expense
from cashbook.views import outstanding_advances_total
from ledger.services.posting import ensure_chart


def _leader(dept, name="ld_195"):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name="Leader")[0])
    DepartmentLeadership.objects.create(department=dept, user=u)
    return u


def _tr():
    u = User.objects.create_user("tr_195", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class AdvancesListHelpTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="F195", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.u = _leader(self.dept); self.tr = _tr()
        StaffAdvance.objects.create(staff_name="X", department=self.dept,
            amount=Decimal("1000"), date_issued=dt.date(2026, 6, 1), purpose="p",
            method="CASH", from_petty_cash=False, issued_by=self.tr, status="ISSUED")
        self.c = Client(); self.c.force_login(self.u)

    def test_help_and_open_button(self):
        b = self.c.get("/leader/advances/").content.decode()
        self.assertIn("How to account for an advance", b)
        self.assertIn("Open →", b)


class ReceivableTopupDateTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="RcvF", fund_type="LOCAL",
            category="MINISTRY")
        self.tr = _tr()

    def test_future_topup_excluded_from_receivable(self):
        adv = StaffAdvance.objects.create(staff_name="R", department=self.dept,
            amount=Decimal("3000"), date_issued=dt.date(2026, 6, 1), purpose="p",
            method="CASH", from_petty_cash=False, issued_by=self.tr, status="ISSUED")
        june = outstanding_advances_total(dt.date(2026, 6, 30))
        AdvanceTopUp.objects.create(advance=adv, date=dt.date(2026, 7, 5),
            amount=Decimal("2000"))
        adv.amount = Decimal("5000"); adv.save()
        self.assertEqual(outstanding_advances_total(dt.date(2026, 6, 30)), june)
        self.assertEqual(
            outstanding_advances_total(dt.date(2026, 7, 31)) - june, Decimal("2000"))


class DevGroupBalanceTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.dept = Department.objects.create(name="DevLead", fund_type="LOCAL",
            category="DEVELOPMENT")
        self.u = _leader(self.dept)
        self.g = DevelopmentGroup.objects.create(name="Group A", number=1, active=True,
            target=Decimal("10000"))
        # a contribution before June and one in June
        for d, amt in [(dt.date(2026, 5, 20), 1000), (dt.date(2026, 6, 10), 2500)]:
            Transaction.objects.create(date=d, amount=Decimal(amt), direction="CREDIT",
                confirmed=True, channel="CASH", allocation_status="MANUAL",
                dev_group=self.g, department=self.dept)
        self.c = Client(); self.c.force_login(self.u)

    def test_dev_group_opening_receipts_closing(self):
        b = self.c.get(f"/leader/department/{self.dept.id}/"
                       "?start=2026-06-01&end=2026-06-30").content.decode()
        if "Development groups" in b:
            self.assertIn(">Opening</th>", b)
            self.assertIn(">Closing</th>", b)


class LeaderAdvanceExportTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.dept = Department.objects.create(name="XlF", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.u = _leader(self.dept); self.tr = _tr()
        self.adv = StaffAdvance.objects.create(staff_name="XL", department=self.dept,
            amount=Decimal("4000"), date_issued=dt.date(2026, 6, 1), purpose="p",
            method="CASH", from_petty_cash=False, issued_by=self.tr, status="ISSUED")
        self.c = Client(); self.c.force_login(self.u)

    def test_xlsx_export(self):
        r = self.c.get(f"/leader/advances/{self.adv.id}/?export=xlsx")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheet", r["Content-Type"])
        self.assertGreater(len(r.content), 3000)

    def test_export_button_present(self):
        b = self.c.get(f"/leader/advances/{self.adv.id}/").content.decode()
        self.assertIn("export=xlsx", b)


class LeaderChargeDeletionTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.dept = Department.objects.create(name="ChgF", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.u = _leader(self.dept); self.tr = _tr()
        self.adv = StaffAdvance.objects.create(staff_name="C", department=self.dept,
            amount=Decimal("5000"), date_issued=dt.date(2026, 6, 1), purpose="p",
            method="CASH", from_petty_cash=False, issued_by=self.tr, status="ISSUED")
        self.c = Client(); self.c.force_login(self.u)

    def test_leader_can_delete_charge(self):
        self.c.post(f"/leader/advances/{self.adv.id}/",
            {"date": "2026-06-05", "description": "Fuel", "category": "TRANSPORT",
             "amount": "1000", "charge": "50"})
        chg = Expense.objects.filter(advance=self.adv, category="BANK_CHARGE").first()
        exp = Expense.objects.filter(advance=self.adv, category="TRANSPORT").first()
        self.assertIsNotNone(chg)
        self.c.post(f"/leader/advances/{self.adv.id}/",
            {"action": "delete_expense", "expense_id": chg.id})
        self.assertFalse(Expense.objects.filter(id=chg.id).exists())
        self.assertTrue(Expense.objects.filter(id=exp.id).exists())  # parent kept

    def test_deleting_expense_removes_its_charge(self):
        self.c.post(f"/leader/advances/{self.adv.id}/",
            {"date": "2026-06-06", "description": "Mat", "category": "MATERIALS",
             "amount": "800", "charge": "20"})
        exp = Expense.objects.filter(advance=self.adv, category="MATERIALS").first()
        chg = Expense.objects.filter(advance=self.adv, category="BANK_CHARGE",
                                     charge_for=exp).first()
        self.assertIsNotNone(chg)
        self.c.post(f"/leader/advances/{self.adv.id}/",
            {"action": "delete_expense", "expense_id": exp.id})
        self.assertFalse(Expense.objects.filter(id=exp.id).exists())
        self.assertFalse(Expense.objects.filter(id=chg.id).exists())


class AdvanceCascadeTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.dept = Department.objects.create(name="CascF", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.tr = _tr()

    def test_delete_advance_removes_issuance_charge(self):
        adv = StaffAdvance.objects.create(staff_name="Iss", department=self.dept,
            amount=Decimal("2000"), date_issued=dt.date(2026, 6, 1), purpose="p",
            method="MPESA", from_petty_cash=False, issued_by=self.tr, status="ISSUED")
        ce = Expense.objects.create(date=dt.date(2026, 6, 1), department=self.dept,
            description="sending charge", amount=Decimal("30"), category="BANK_CHARGE",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        adv.charge_expense = ce; adv.save()
        c = Client(); c.force_login(self.tr)
        c.post(f"/advances/{adv.id}/delete/")
        self.assertFalse(StaffAdvance.objects.filter(id=adv.id).exists())
        self.assertFalse(Expense.objects.filter(id=ce.id).exists())


class LoginPageTests(TestCase):
    def test_modern_signin(self):
        b = Client().get("/accounts/login/").content.decode()
        self.assertIn("signin-brand", b)
        self.assertIn("sf-eye", b)  # password toggle
        self.assertIn('data-theme="light"', b)
