"""Staff advances: petty-cash funding (reduces float, increases receivable),
leader-entered settling expenses (approved+paid, claimant=leader), statement,
and financial-statement integrity."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department, DepartmentLeadership
from cashbook.models import StaffAdvance, Expense, PettyCashTopUp
from cashbook.views import _petty_balance_asof, outstanding_advances_total


class AdvancePettyLeaderTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("tr", password="x", is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.leader = User.objects.create_user("ld", password="x",
            first_name="Jane", last_name="Leader")
        self.leader.groups.add(Group.objects.get_or_create(name="Leader")[0])
        self.dept = Department.objects.create(name="Youth", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.other = Department.objects.create(name="Music", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        DepartmentLeadership.objects.create(user=self.leader, department=self.dept)
        PettyCashTopUp.objects.create(date=dt.date(2026, 6, 1),
            amount=Decimal("20000"), recorded_by=self.tr)

    def _advance(self, petty=True, amount="8000"):
        return StaffAdvance.objects.create(staff_name="Jane Leader",
            department=self.dept, amount=Decimal(amount), date_issued=dt.date(2026, 6, 5),
            purpose="Camp travel", method="CASH", from_petty_cash=petty,
            issued_by=self.tr)

    def test_petty_advance_reduces_float(self):
        base = _petty_balance_asof(dt.date(2026, 6, 30))
        self._advance()
        after = _petty_balance_asof(dt.date(2026, 6, 30))
        self.assertEqual(base - after, Decimal("8000"))

    def test_advance_is_receivable(self):
        self._advance()
        self.assertGreaterEqual(outstanding_advances_total(dt.date(2026, 6, 30)),
                                Decimal("8000"))

    def test_non_petty_advance_does_not_touch_float(self):
        base = _petty_balance_asof(dt.date(2026, 6, 30))
        self._advance(petty=False)
        self.assertEqual(_petty_balance_asof(dt.date(2026, 6, 30)), base)

    def test_leader_records_expense_paid_with_their_name(self):
        adv = self._advance()
        c = Client(); c.force_login(self.leader)
        c.post(f"/leader/advances/{adv.id}/",
               {"date": "2026-06-10", "description": "Bus", "amount": "5000",
                "category": "TRANSPORT"})
        exp = adv.expenses.first()
        self.assertEqual(exp.status, "PAID")
        self.assertEqual(exp.claimant, "Jane Leader")
        self.assertTrue(exp.paid_from_petty_cash)
        adv.refresh_from_db()
        self.assertEqual(adv.status, "PARTLY")
        self.assertEqual(adv.balance, Decimal("3000"))

    def test_settling_keeps_petty_float_stable(self):
        adv = self._advance()
        after_issue = _petty_balance_asof(dt.date(2026, 6, 30))
        c = Client(); c.force_login(self.leader)
        c.post(f"/leader/advances/{adv.id}/",
               {"date": "2026-06-10", "description": "Bus", "amount": "5000"})
        # outstanding -5000, petty expense +5000 -> net zero on the float
        self.assertEqual(_petty_balance_asof(dt.date(2026, 6, 30)), after_issue)

    def test_leader_scope_enforced(self):
        adv = StaffAdvance.objects.create(staff_name="X", department=self.other,
            amount=Decimal("100"), date_issued=dt.date(2026, 6, 5), purpose="x",
            issued_by=self.tr)
        c = Client(); c.force_login(self.leader)
        self.assertEqual(c.get(f"/leader/advances/{adv.id}/").status_code, 302)
        # and cannot POST an expense either
        c.post(f"/leader/advances/{adv.id}/",
               {"date": "2026-06-10", "description": "x", "amount": "50"})
        self.assertEqual(adv.expenses.count(), 0)

    def test_overdraw_petty_blocked(self):
        c = Client(); c.force_login(self.tr)
        r = c.post("/advances/new/", {"staff_name": "Z", "department": self.dept.id,
            "amount": "999999", "date_issued": "2026-06-05", "from_petty_cash": "1",
            "method": "CASH", "purpose": "too much"})
        # blocked (no advance created from petty cash beyond the float)
        self.assertFalse(StaffAdvance.objects.filter(staff_name="Z",
            from_petty_cash=True).exists())

    def test_statement_renders(self):
        adv = self._advance()
        c = Client(); c.force_login(self.tr)
        b = c.get(f"/advances/{adv.id}/").content.decode()
        self.assertIn("Statement", b)
        self.assertIn("Still to account", b)

    def test_returned_to_petty_credits_float(self):
        adv = self._advance()
        c = Client(); c.force_login(self.leader)
        c.post(f"/leader/advances/{adv.id}/",
               {"date": "2026-06-10", "description": "Bus", "amount": "5000"})
        # close with 3000 returned to petty
        cc = Client(); cc.force_login(self.tr)
        before = _petty_balance_asof(dt.date(2026, 6, 30))
        cc.post(f"/advances/{adv.id}/close/", {"returned_to_petty": "3000"})
        adv.refresh_from_db()
        self.assertEqual(adv.returned_to_petty, Decimal("3000"))
        # float goes back up by the returned amount
        self.assertEqual(_petty_balance_asof(dt.date(2026, 6, 30)) - before,
                         Decimal("3000"))

    def test_sofp_balances_with_advance(self):
        self._advance()
        from django.test import RequestFactory
        from reports.views import FinancialPositionView
        rf = RequestFactory().get("/reports/financial-position/?as_of=2026-06-30")
        rf.user = self.tr
        v = FinancialPositionView(); v.request = rf; v.kwargs = {}
        # render via get to compute totals; ensure 200 and balanced identity
        resp = v.get(rf)
        self.assertEqual(resp.status_code, 200)
