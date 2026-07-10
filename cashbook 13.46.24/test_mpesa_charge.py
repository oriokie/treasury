"""M-Pesa / bank transaction charges recorded against an expense are created as
a linked bank-charge expense (charge_for) — from the manual form and the import."""
import io
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from django.core.files.uploadedfile import SimpleUploadedFile

from departments.models import Department, expense_departments
from cashbook.models import Expense
from core.models import SiteConfig


def _treasurer():
    u = User.objects.create_user("ch_treas", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class MpesaChargeTests(TestCase):
    def setUp(self):
        cfg = SiteConfig.get(); cfg.require_expense_approval = False; cfg.save()
        self.u = _treasurer()
        self.fund = Department.objects.create(name="LCB Fund", fund_type="LOCAL",
                                              category="OFFERING", selectable=True,
                                              show_in_expenses=True)
        self.c = Client(); self.c.force_login(self.u)

    def _expense_dept(self):
        funds = list(expense_departments())
        return funds[0] if funds else self.fund

    def test_manual_charge_creates_linked_expense(self):
        d = self._expense_dept()
        self.c.post("/expenses/new/", {
            "date": "2026-06-10", "department": str(d.id), "description": "Airtime",
            "amount": "1000", "category": "OTHER", "method": "MPESA",
            "expenditure_type": "RECURRENT", "charge": "30", "override_balance": "1"})
        parent = Expense.objects.get(description="Airtime")
        charge = Expense.objects.get(charge_for=parent)
        self.assertEqual(charge.amount, Decimal("30"))
        self.assertEqual(charge.category, "BANK_CHARGE")
        self.assertEqual(charge.department_id, parent.department_id)

    def test_no_charge_no_extra_expense(self):
        d = self._expense_dept()
        self.c.post("/expenses/new/", {
            "date": "2026-06-10", "department": str(d.id), "description": "No charge item",
            "amount": "500", "category": "OTHER", "method": "CASH",
            "expenditure_type": "RECURRENT", "override_balance": "1"})
        parent = Expense.objects.get(description="No charge item")
        self.assertEqual(parent.charges.count(), 0)

    def _xlsx(self, rows):
        import openpyxl
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Expenses"
        ws.append(["Date", "Fund", "Description", "Amount", "Category", "Method",
                   "Claimant", "Voucher no", "M-Pesa charge"])
        for r in rows:
            ws.append(r)
        buf = io.BytesIO(); wb.save(buf)
        return SimpleUploadedFile("e.xlsx", buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def test_import_charge_creates_linked_expense(self):
        up = self._xlsx([["2026-06-11", self.fund.name, "Printer ink", 3000,
                          "Materials", "M-Pesa", "X", "V9", 50]])
        self.c.post("/expenses/import/", {"file": up})
        self.c.post("/expenses/import/", {"apply": "1"})
        parent = Expense.objects.get(description="Printer ink")
        charge = Expense.objects.get(charge_for=parent)
        self.assertEqual(charge.amount, Decimal("50"))
        self.assertEqual(charge.category, "BANK_CHARGE")

    def test_import_blank_charge_no_extra(self):
        up = self._xlsx([["2026-06-11", self.fund.name, "Chairs", 8000,
                          "Materials", "Cash", "Y", "V10", ""]])
        self.c.post("/expenses/import/", {"file": up})
        self.c.post("/expenses/import/", {"apply": "1"})
        parent = Expense.objects.get(description="Chairs")
        self.assertEqual(parent.charges.count(), 0)

    def test_template_has_charge_column(self):
        r = self.c.get("/expenses/import/?download=1")
        self.assertEqual(r.status_code, 200)
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        header = [c.value for c in wb["Expenses"][1]]
        self.assertIn("M-Pesa charge", header)
