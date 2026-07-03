"""Bank reconciliation must auto-add BOTH bank-funded and petty-cash-funded
outstanding staff advances. Previously, petty-funded advances silently
disappeared: the petty float already subtracted them, but nothing added them
back as their own reconciling item, so the reconciliation worksheet was short
by exactly that amount."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import StaffAdvance, PettyCashTopUp
from statements.models import BankReconciliation


def _tr():
    u = User.objects.create_user("tr_recon_adv", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class PettyAdvanceReconciliationTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="ReconAdvF", fund_type="LOCAL",
            category="MINISTRY")
        PettyCashTopUp.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("20000"),
            recorded_by=self.tr)
        self.adv = StaffAdvance.objects.create(staff_name="Petty Adv Recon",
            department=self.d, amount=Decimal("6000"), date_issued=dt.date(2026, 6, 5),
            purpose="x", method="CASH", from_petty_cash=True,
            issued_by=self.tr, status="ISSUED")
        self.c = Client(); self.c.force_login(self.tr)

    def test_petty_advance_item_created(self):
        self.c.post("/reconciliations/new/",
            {"statement_date": "2026-06-30", "bank_balance": "500000"})
        rec = BankReconciliation.objects.order_by("-id").first()
        items = {it.description: it.amount for it in rec.items.all()}
        self.assertEqual(items.get("Staff advances from petty cash (not yet accounted)"),
                         Decimal("6000"))

    def test_petty_float_and_advance_together_equal_full_topup(self):
        self.c.post("/reconciliations/new/",
            {"statement_date": "2026-06-30", "bank_balance": "500000"})
        rec = BankReconciliation.objects.order_by("-id").first()
        items = {it.description: it.amount for it in rec.items.all()}
        total = ((items.get("Petty cash float (cash on hand)") or Decimal(0))
                 + (items.get("Staff advances from petty cash (not yet accounted)") or Decimal(0)))
        self.assertEqual(total, Decimal("20000"))

    def test_bank_funded_advance_still_works(self):
        bank_adv = StaffAdvance.objects.create(staff_name="Bank Adv Recon",
            department=self.d, amount=Decimal("8000"), date_issued=dt.date(2026, 6, 1),
            purpose="x", method="MPESA", from_petty_cash=False,
            issued_by=self.tr, status="ISSUED")
        self.c.post("/reconciliations/new/",
            {"statement_date": "2026-06-30", "bank_balance": "500000"})
        rec = BankReconciliation.objects.order_by("-id").first()
        items = {it.description: it.amount for it in rec.items.all()}
        self.assertEqual(items.get("Staff advances issued (not yet accounted)"),
                         Decimal("8000"))

    def test_topup_after_statement_date_excluded(self):
        self.c.post(f"/advances/{self.adv.id}/topup/",
            {"date": "2026-07-05", "amount": "2000"})
        self.c.post("/reconciliations/new/",
            {"statement_date": "2026-06-30", "bank_balance": "500000"})
        rec = BankReconciliation.objects.order_by("-id").first()
        items = {it.description: it.amount for it in rec.items.all()}
        # topup dated after the statement hadn't happened yet as of 30 Jun
        self.assertEqual(items.get("Staff advances from petty cash (not yet accounted)"),
                         Decimal("6000"))
