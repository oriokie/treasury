"""Review finding: the Staff Advances figure used during bank reconciliation
was reported as possibly needing to show "Pending (Not Accounted For)"
instead of its current value. Traced the calculation through
_sync_managed_recon_items() -> outstanding_bank_advances_total() /
outstanding_petty_advances_total() and confirmed it already computes exactly
that: (amount advanced as of the statement date) minus (expenses already
settled against it) minus (returns/top-ups), i.e. the outstanding balance not
yet accounted for — using the reconciliation's own statement_date, not
"today". Cross-checked against the SOFP and dashboard, which use the same
underlying concept via outstanding_advances_total(). No defect found; this
test documents and locks in the verified-correct behaviour."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import StaffAdvance, Expense
from cashbook.views import outstanding_bank_advances_total
from statements.models import BankReconciliation


def _tr():
    u = User.objects.create_user("tr_advrecon", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class StaffAdvancesReconciliationFigureTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="AdvReconFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_shows_outstanding_not_yet_accounted_for_balance(self):
        adv = StaffAdvance.objects.create(staff_name="Review Advance", department=self.d,
            amount=Decimal("5000"), date_issued=dt.date(2026, 6, 1), purpose="trip",
            method="CASH", from_petty_cash=False, issued_by=self.tr, status="ISSUED")
        Expense.objects.create(advance=adv, department=self.d, description="partial spend",
            amount=Decimal("2000"), category="OTHER", status="PAID", recorded_by=self.tr,
            approved_by=self.tr, date=dt.date(2026, 6, 5))
        # 5000 advanced - 2000 already accounted for = 3000 still pending
        self.assertEqual(outstanding_bank_advances_total(dt.date(2026, 6, 30)),
                         Decimal("3000"))

    def test_fully_accounted_advance_contributes_nothing(self):
        adv = StaffAdvance.objects.create(staff_name="Settled Advance", department=self.d,
            amount=Decimal("1000"), date_issued=dt.date(2026, 6, 1), purpose="trip",
            method="CASH", from_petty_cash=False, issued_by=self.tr, status="ISSUED")
        Expense.objects.create(advance=adv, department=self.d, description="full spend",
            amount=Decimal("1000"), category="OTHER", status="PAID", recorded_by=self.tr,
            approved_by=self.tr, date=dt.date(2026, 6, 5))
        self.assertEqual(outstanding_bank_advances_total(dt.date(2026, 6, 30)), Decimal("0"))

    def test_uses_the_reconciliation_statement_date_not_today(self):
        adv = StaffAdvance.objects.create(staff_name="Dated Advance", department=self.d,
            amount=Decimal("2000"), date_issued=dt.date(2026, 6, 1), purpose="trip",
            method="CASH", from_petty_cash=False, issued_by=self.tr, status="ISSUED")
        # an expense settling part of it, dated AFTER the statement date, must
        # not count as "already accounted for" as of that earlier date
        Expense.objects.create(advance=adv, department=self.d, description="late spend",
            amount=Decimal("500"), category="OTHER", status="PAID", recorded_by=self.tr,
            approved_by=self.tr, date=dt.date(2026, 7, 15))
        self.assertEqual(outstanding_bank_advances_total(dt.date(2026, 6, 30)),
                         Decimal("2000"))
        # but as of a later date, once that expense has happened, it does count
        self.assertEqual(outstanding_bank_advances_total(dt.date(2026, 7, 31)),
                         Decimal("1500"))

    def test_reconciliation_worksheet_shows_the_correct_managed_item(self):
        adv = StaffAdvance.objects.create(staff_name="Worksheet Advance", department=self.d,
            amount=Decimal("4000"), date_issued=dt.date(2026, 6, 1), purpose="trip",
            method="CASH", from_petty_cash=False, issued_by=self.tr, status="ISSUED")
        Expense.objects.create(advance=adv, department=self.d, description="spend",
            amount=Decimal("1500"), category="OTHER", status="PAID", recorded_by=self.tr,
            approved_by=self.tr, date=dt.date(2026, 6, 5))
        rec = BankReconciliation.objects.create(statement_date=dt.date(2026, 6, 30),
            bank_balance=Decimal("10000"), created_by=self.tr)
        c = Client(); c.force_login(self.tr)
        b = c.get(f"/reconciliations/{rec.id}/").content.decode()
        self.assertIn("not yet accounted", b)
        item = rec.items.filter(description__icontains="staff advance").first()
        self.assertIsNotNone(item)
        self.assertEqual(item.amount, Decimal("2500"))   # 4000 - 1500
