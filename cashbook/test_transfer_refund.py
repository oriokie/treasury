"""#1 Transfer editing + #2 Expense refunds (contra-entry)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import Expense, ExpenseRefund, FundTransfer
from reports.services.balances import fund_balance
from ledger.models import JournalLine
from ledger.services.posting import ensure_chart


def _treasurer(username="tr"):
    u = User.objects.create_user(username, password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class TransferEditTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)
        self.a = Department.objects.create(name="A", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("10000"))
        self.b = Department.objects.create(name="B", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("0"))
        self.t = FundTransfer.objects.create(date=dt.date(2026, 6, 1),
            source=self.a, destination=self.b, amount=Decimal("3000"),
            recorded_by=self.tr)

    def test_edit_resyncs_balances_and_journal(self):
        self.c.post(f"/transfers/{self.t.id}/edit/", {"date": "2026-06-01",
            "source": self.a.id, "destination": self.b.id, "amount": "1000",
            "reason": "fixed", "reference": ""})
        self.t.refresh_from_db()
        self.assertEqual(self.t.amount, Decimal("1000"))
        self.assertEqual(fund_balance(self.a), Decimal("9000"))
        self.assertEqual(fund_balance(self.b), Decimal("1000"))
        lines = JournalLine.objects.filter(entry__source_type="transfer",
            entry__source_id=self.t.pk)
        self.assertEqual(lines.count(), 2)
        self.assertEqual(max(max(l.debit, l.credit) for l in lines), Decimal("1000"))

    def test_history_records_edit(self):
        self.c.post(f"/transfers/{self.t.id}/edit/", {"date": "2026-06-01",
            "source": self.a.id, "destination": self.b.id, "amount": "1500",
            "reason": "", "reference": ""})
        self.assertGreaterEqual(self.t.history.count(), 2)

    def test_reversed_cannot_be_edited(self):
        self.t.reverse(self.tr)
        self.assertEqual(self.c.get(f"/transfers/{self.t.id}/edit/").status_code, 302)
        self.assertTrue(self.t.is_locked)


class ExpenseRefundTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _treasurer("tr2")
        self.c = Client(); self.c.force_login(self.tr)
        self.f = Department.objects.create(name="Cups", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("20000"),
            show_in_expenses=True)
        self.e = Expense.objects.create(date=dt.date(2026, 6, 2), department=self.f,
            description="Cups", amount=Decimal("5000"), category="MATERIALS",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)

    def test_refund_preserves_expense_and_restores_balance(self):
        self.assertEqual(fund_balance(self.f), Decimal("15000"))
        self.c.post(f"/expenses/{self.e.id}/refund/", {"date": "2026-06-05",
            "amount": "800", "method": "CASH", "note": "change"})
        self.e.refresh_from_db()
        self.assertEqual(self.e.amount, Decimal("5000"))       # unchanged
        self.assertEqual(self.e.net_amount, Decimal("4200"))   # net cost
        self.assertEqual(fund_balance(self.f), Decimal("15800"))  # restored

    def test_refund_posts_contra_journal(self):
        self.c.post(f"/expenses/{self.e.id}/refund/",
                    {"date": "2026-06-05", "amount": "800", "method": "CASH"})
        lines = JournalLine.objects.filter(entry__source_type="refund")
        self.assertEqual(lines.count(), 2)

    def test_cannot_over_refund(self):
        self.c.post(f"/expenses/{self.e.id}/refund/",
                    {"date": "2026-06-05", "amount": "800", "method": "CASH"})
        self.c.post(f"/expenses/{self.e.id}/refund/",
                    {"date": "2026-06-05", "amount": "5000", "method": "CASH"})
        self.assertEqual(ExpenseRefund.objects.filter(expense=self.e).count(), 1)

    def test_cannot_refund_pending_expense(self):
        pend = Expense.objects.create(date=dt.date(2026, 6, 2), department=self.f,
            description="Y", amount=Decimal("1000"), category="MATERIALS",
            status="PENDING", recorded_by=self.tr)
        self.c.post(f"/expenses/{pend.id}/refund/",
                    {"date": "2026-06-05", "amount": "100", "method": "CASH"})
        self.assertEqual(ExpenseRefund.objects.filter(expense=pend).count(), 0)

    def test_refund_is_date_aware_in_balance(self):
        self.c.post(f"/expenses/{self.e.id}/refund/",
                    {"date": "2026-06-05", "amount": "800", "method": "CASH"})
        self.assertEqual(fund_balance(self.f, dt.date(2026, 6, 4)), Decimal("15000"))
        self.assertEqual(fund_balance(self.f, dt.date(2026, 6, 5)), Decimal("15800"))
