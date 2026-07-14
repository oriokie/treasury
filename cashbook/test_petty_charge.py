"""Petty cash disbursements mirror the expense form: method + M-Pesa/bank charge,
and the charge also reduces the float. Import supports a petty-cash column."""
import io
from decimal import Decimal
import datetime as dt

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from django.core.files.uploadedfile import SimpleUploadedFile

from departments.models import Department, expense_departments
from cashbook.models import Expense
from cashbook.views import _petty_balance_asof
from core.models import SiteConfig


class PettyChargeTests(TestCase):
    def setUp(self):
        cfg = SiteConfig.get()
        cfg.require_expense_approval = False; cfg.enforce_petty_float = False
        # The retired disbursement form bypassed the fund-balance check
        # entirely; the expense form (correctly) enforces it. This test is
        # about the petty-cash charge mechanics, not about overspend.
        cfg.enforce_fund_balance = False; cfg.save()
        u = User.objects.create_user("pcc", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)
        self.fund = Department.objects.create(name="LCB Fund", fund_type="LOCAL",
                                              category="OFFERING", show_in_expenses=True)

    def test_disburse_with_charge_links_and_reduces_float(self):
        """The separate "record a disbursement" form is retired — it wrote the
        same Expense the expense form writes, but could not attach a receipt,
        set an expenditure type or a budget line. The capability is unchanged;
        it just lives in one form now, which is what this proves."""
        on = dt.date(2026, 6, 15)
        b0 = _petty_balance_asof(on)
        self.c.post("/expenses/new/", {
            "date": "2026-06-15", "description": "Tape", "amount": "200",
            "department": str(self.fund.id), "category": "MATERIALS",
            "method": "MPESA", "voucher_no": "PC1", "charge": "30",
            "paid_from_petty_cash": "on", "expenditure_type": "RECURRENT"})
        main = Expense.objects.get(description="Tape", paid_from_petty_cash=True)
        charge = Expense.objects.get(charge_for=main)
        self.assertTrue(charge.paid_from_petty_cash)
        self.assertEqual(charge.amount, Decimal("30"))
        self.assertEqual(charge.category, "BANK_CHARGE")
        # both reduce the float -> 230 total
        self.assertEqual(b0 - _petty_balance_asof(on), Decimal("230.00"))

    def test_disburse_no_charge(self):
        self.c.post("/expenses/new/", {
            "date": "2026-06-15", "description": "Pens", "amount": "100",
            "department": str(self.fund.id), "category": "MATERIALS",
            "method": "CASH", "paid_from_petty_cash": "on",
            "expenditure_type": "RECURRENT"})
        main = Expense.objects.get(description="Pens", paid_from_petty_cash=True)
        self.assertEqual(main.charges.count(), 0)

    def test_import_petty_column(self):
        wb_bytes = self._xlsx([["2026-06-16", self.fund.name, "Glue", 150,
                                "Materials", "Cash", "Z", "V20", "", "Yes"]])
        self.c.post("/expenses/import/", {"file": wb_bytes})
        self.c.post("/expenses/import/", {"apply": "1"})
        imp = Expense.objects.get(description="Glue")
        self.assertTrue(imp.paid_from_petty_cash)
        self.assertEqual(imp.status, "PAID")

    def test_expense_form_has_petty_checkbox(self):
        r = self.c.get("/expenses/new/")
        self.assertIn("paid_from_petty_cash", r.content.decode())

    def _xlsx(self, rows):
        import openpyxl
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Expenses"
        ws.append(["Date", "Fund", "Description", "Amount", "Category", "Method",
                   "Claimant", "Voucher no", "M-Pesa charge", "Paid from petty cash"])
        for r in rows:
            ws.append(r)
        buf = io.BytesIO(); wb.save(buf)
        return SimpleUploadedFile("e.xlsx", buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
