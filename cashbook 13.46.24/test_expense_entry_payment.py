"""Issue a payment (cheque/EFT/M-Pesa/etc.) directly during expense entry."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from core.models import SiteConfig
from departments.models import Department
from cashbook.models import Expense, PaymentInstrument
from ledger.services.posting import ensure_chart


def _user(name, group):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=group)[0])
    return u


class ExpenseEntryPaymentTests(TestCase):
    def setUp(self):
        ensure_chart()
        cfg = SiteConfig.get()
        cfg.require_expense_approval = False
        cfg.enforce_fund_balance = False
        cfg.save()
        self.tr = _user("tr_ep", "Treasurer")
        self.tr.is_superuser = True; self.tr.save()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="LCB EP", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)

    def _post(self, **extra):
        data = {"date": "2026-06-15", "department": self.d.id,
                "description": "Test expense", "amount": "1500",
                "category": "STATIONERY", "method": "CHEQUE",
                "issue_payment": "1", "payment_method": "CHEQUE",
                "payment_reference": "CHQ900", "payment_date": "2026-06-15"}
        data.update(extra)
        return self.c.post("/expenses/new/", data)

    def test_checkbox_present_when_auto_approve(self):
        body = self.c.get("/expenses/new/").content.decode()
        self.assertIn('name="issue_payment"', body)
        self.assertIn('name="payment_method"', body)

    def test_payment_created_and_linked(self):
        n0 = PaymentInstrument.objects.count()
        self._post()
        exp = Expense.objects.get(description="Test expense")
        self.assertEqual(exp.status, "APPROVED")
        inst = PaymentInstrument.objects.filter(expense=exp).first()
        self.assertIsNotNone(inst)
        self.assertEqual(PaymentInstrument.objects.count(), n0 + 1)
        self.assertEqual(inst.method, "CHEQUE")
        self.assertEqual(inst.instrument_number, "CHQ900")
        self.assertEqual(inst.status, "ISSUED")
        self.assertEqual(inst.amount, Decimal("1500"))
        self.assertEqual(inst.source_kind, "EXPENSE")

    def test_no_payment_without_checkbox(self):
        n0 = PaymentInstrument.objects.count()
        self._post(issue_payment="")
        exp = Expense.objects.get(description="Test expense")
        self.assertEqual(PaymentInstrument.objects.count(), n0)
        self.assertFalse(PaymentInstrument.objects.filter(expense=exp).exists())

    def test_detail_page_shows_linked_payment(self):
        self._post()
        exp = Expense.objects.get(description="Test expense")
        body = self.c.get(f"/expenses/{exp.id}/").content.decode()
        self.assertIn("CHQ900", body)
        self.assertIn("open register", body)


class ExpenseEntryPaymentPermissionTests(TestCase):
    def setUp(self):
        ensure_chart()
        cfg = SiteConfig.get()
        cfg.require_expense_approval = True
        cfg.enforce_fund_balance = False
        cfg.save()
        self.d = Department.objects.create(name="LCB EP2", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)

    def test_assistant_cannot_issue_when_approval_required(self):
        asst = _user("asst_ep", "Assistant")
        c = Client(); c.force_login(asst)
        body = c.get("/expenses/new/").content.decode()
        self.assertNotIn('name="issue_payment"', body)
        self.assertIn("requires approval before payment", body)

    def test_treasurer_can_self_approve_and_issue_when_approval_required(self):
        tr = _user("tr_ep2", "Treasurer"); tr.is_superuser = True; tr.save()
        c = Client(); c.force_login(tr)
        body = c.get("/expenses/new/").content.decode()
        self.assertIn('name="issue_payment"', body)
        c.post("/expenses/new/", {
            "date": "2026-06-15", "department": self.d.id,
            "description": "Treasurer direct pay", "amount": "800",
            "category": "MAINTENANCE", "method": "CHEQUE",
            "issue_payment": "1", "payment_method": "EFT",
            "payment_reference": "EFT700", "payment_date": "2026-06-15"})
        exp = Expense.objects.get(description="Treasurer direct pay")
        self.assertEqual(exp.status, "APPROVED")
        inst = PaymentInstrument.objects.filter(expense=exp).first()
        self.assertIsNotNone(inst)
        self.assertEqual(inst.method, "EFT")

    def test_normal_pending_flow_unaffected(self):
        asst = _user("asst_ep2", "Assistant")
        c = Client(); c.force_login(asst)
        c.post("/expenses/new/", {"date": "2026-06-15", "department": self.d.id,
            "description": "Plain claim", "amount": "300",
            "category": "OTHER", "method": "CASH"})
        exp = Expense.objects.get(description="Plain claim")
        self.assertEqual(exp.status, "PENDING")
        self.assertFalse(PaymentInstrument.objects.filter(expense=exp).exists())
