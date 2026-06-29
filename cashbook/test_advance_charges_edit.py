"""Advance bank charges (#1), edit/delete end-to-end (#3), bank-advance
reconciling item (#2), petty register reflects advances (#6), executive
tiles (#8), leader nav single-dept (#4)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department, DepartmentLeadership
from cashbook.models import StaffAdvance, Expense, PettyCashTopUp
from cashbook.views import (_petty_balance_asof, outstanding_advances_total,
                            outstanding_bank_advances_total)


class AdvanceChargeEditTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("tr", password="x", is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.dept = Department.objects.create(name="Youth", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.c = Client(); self.c.force_login(self.tr)

    def _make(self, **kw):
        data = {"staff_name": "Sam", "department": self.dept.id, "amount": "6000",
                "date_issued": "2026-06-05", "method": "MPESA", "purpose": "trip"}
        data.update(kw)
        self.c.post("/advances/new/", data)
        return StaffAdvance.objects.filter(staff_name=data["staff_name"]).first()

    def test_bank_charge_reduces_advance(self):
        adv = self._make(bank_charge="55")
        self.assertEqual(adv.bank_charge, Decimal("55"))
        self.assertIsNotNone(adv.charge_expense)
        self.assertEqual(adv.charge_expense.category, "BANK_CHARGE")
        self.assertEqual(adv.charge_expense.status, "PAID")
        # the charge is met out of the advance, so it reduces the balance
        self.assertEqual(adv.settled_total, Decimal("55"))
        self.assertEqual(adv.balance, Decimal("6000") - Decimal("55"))

    def test_edit_syncs_charge_and_amount(self):
        adv = self._make(bank_charge="55")
        self.c.post(f"/advances/{adv.id}/edit/", {"staff_name": "Sam",
            "department": self.dept.id, "amount": "7000", "date_issued": "2026-06-05",
            "method": "MPESA", "bank_charge": "80", "purpose": "trip2"})
        adv.refresh_from_db()
        self.assertEqual(adv.amount, Decimal("7000"))
        self.assertEqual(adv.charge_expense.amount, Decimal("80"))

    def test_removing_charge_deletes_expense(self):
        adv = self._make(bank_charge="55")
        cid = adv.charge_expense_id
        self.c.post(f"/advances/{adv.id}/edit/", {"staff_name": "Sam",
            "department": self.dept.id, "amount": "6000", "date_issued": "2026-06-05",
            "method": "MPESA", "bank_charge": "0", "purpose": "trip"})
        adv.refresh_from_db()
        self.assertIsNone(adv.charge_expense)
        self.assertFalse(Expense.objects.filter(id=cid).exists())

    def test_delete_is_end_to_end(self):
        adv = self._make(bank_charge="55")
        Expense.objects.create(date=dt.date(2026, 6, 10), department=self.dept,
            description="settle", amount=Decimal("1000"), status="PAID",
            recorded_by=self.tr, advance=adv)
        cid = adv.charge_expense_id
        self.c.post(f"/advances/{adv.id}/delete/", {})
        self.assertFalse(StaffAdvance.objects.filter(id=adv.id).exists())
        self.assertFalse(Expense.objects.filter(advance_id=adv.id).exists())
        self.assertFalse(Expense.objects.filter(id=cid).exists())

    def test_closed_advance_amend_treasurer_only(self):
        adv = self._make()
        adv.status = "CLOSED"; adv.save()
        # assistant (data entry, not treasurer) blocked
        asst = User.objects.create_user("asst", password="x")
        asst.groups.add(Group.objects.get_or_create(name="Assistant")[0])
        c2 = Client(); c2.force_login(asst)
        c2.post(f"/advances/{adv.id}/edit/", {"staff_name": "Changed",
            "department": self.dept.id, "amount": "6000", "date_issued": "2026-06-05",
            "method": "MPESA", "purpose": "trip"})
        adv.refresh_from_db()
        self.assertEqual(adv.staff_name, "Sam")   # unchanged
        # treasurer can
        self.c.post(f"/advances/{adv.id}/edit/", {"staff_name": "Fixed",
            "department": self.dept.id, "amount": "6000", "date_issued": "2026-06-05",
            "method": "MPESA", "purpose": "trip"})
        adv.refresh_from_db()
        self.assertEqual(adv.staff_name, "Fixed")


class BankAdvanceReconTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("trb", password="x", is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.dept = Department.objects.create(name="Op", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.c = Client(); self.c.force_login(self.tr)

    def test_bank_advances_exclude_petty(self):
        StaffAdvance.objects.create(staff_name="B", department=self.dept,
            amount=Decimal("12000"), date_issued=dt.date(2026, 6, 5), purpose="x",
            method="BANK", from_petty_cash=False, issued_by=self.tr)
        PettyCashTopUp.objects.create(date=dt.date(2026, 6, 1),
            amount=Decimal("20000"), recorded_by=self.tr)
        StaffAdvance.objects.create(staff_name="P", department=self.dept,
            amount=Decimal("3000"), date_issued=dt.date(2026, 6, 6), purpose="y",
            method="CASH", from_petty_cash=True, issued_by=self.tr)
        self.assertEqual(outstanding_bank_advances_total(dt.date(2026, 6, 30)),
                         Decimal("12000"))

    def test_add_advances_reconciling_item(self):
        from statements.models import BankReconciliation, ReconciliationItem
        StaffAdvance.objects.create(staff_name="B", department=self.dept,
            amount=Decimal("12000"), date_issued=dt.date(2026, 6, 5), purpose="x",
            method="BANK", from_petty_cash=False, issued_by=self.tr)
        rec = BankReconciliation.objects.create(statement_date=dt.date(2026, 6, 30),
            bank_balance=Decimal("80000"), created_by=self.tr)
        before = rec.adjusted_balance
        self.c.post(f"/reconciliations/{rec.id}/", {"action": "add_advances"})
        it = ReconciliationItem.objects.filter(reconciliation=rec,
            description__icontains="staff advance").first()
        self.assertIsNotNone(it)
        self.assertEqual(it.effect, "ADD")
        self.assertEqual(it.amount, Decimal("12000"))
        rec.refresh_from_db()
        self.assertEqual(rec.adjusted_balance - before, Decimal("12000"))


class PettyRegisterAdvanceTests(TestCase):
    def test_petty_advance_shows_in_register_and_reconciles(self):
        tr = User.objects.create_user("trp", password="x", is_superuser=True)
        tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        dept = Department.objects.create(name="Z", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        PettyCashTopUp.objects.create(date=dt.date(2026, 7, 1),
            amount=Decimal("30000"), recorded_by=tr)
        StaffAdvance.objects.create(staff_name="Reg", department=dept,
            amount=Decimal("9000"), date_issued=dt.date(2026, 7, 3), purpose="trip",
            method="CASH", from_petty_cash=True, issued_by=tr)
        c = Client(); c.force_login(tr)
        r = c.get("/petty-cash/?start=2026-07-01&end=2026-07-31")
        self.assertContains(r, "Advance to Reg")
        # register closing reconciles to the float
        self.assertEqual(r.context["closing"], _petty_balance_asof(dt.date(2026, 7, 31)))


class ExecutiveTilesTests(TestCase):
    def test_tiles_present(self):
        u = User.objects.create_user("ex", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        c = Client(); c.force_login(u)
        b = c.get("/executive/").content.decode()
        self.assertIn("Staff advances outstanding", b)
        self.assertIn("Petty cash remaining", b)


class LeaderSingleDeptTests(TestCase):
    def test_single_dept_redirects(self):
        leader = User.objects.create_user("lone", password="x", first_name="L")
        leader.groups.add(Group.objects.get_or_create(name="Leader")[0])
        d = Department.objects.create(name="Solo", fund_type="LOCAL",
            category="MINISTRY")
        DepartmentLeadership.objects.create(user=leader, department=d)
        c = Client(); c.force_login(leader)
        r = c.get("/leader/")
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"/leader/department/{d.id}/", r.url)


class AutoReconAndChargeTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("tra", password="x", is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.dept = Department.objects.create(name="A", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        self.c = Client(); self.c.force_login(self.tr)

    def test_recon_auto_populates_on_view(self):
        from statements.models import BankReconciliation, ReconciliationItem
        PettyCashTopUp.objects.create(date=dt.date(2026, 6, 1),
            amount=Decimal("20000"), recorded_by=self.tr)
        StaffAdvance.objects.create(staff_name="B", department=self.dept,
            amount=Decimal("7000"), date_issued=dt.date(2026, 6, 3), purpose="x",
            method="BANK", from_petty_cash=False, issued_by=self.tr)
        rec = BankReconciliation.objects.create(statement_date=dt.date(2026, 6, 30),
            bank_balance=Decimal("50000"), created_by=self.tr)
        # merely viewing populates the managed items
        self.c.get(f"/reconciliations/{rec.id}/")
        petty = ReconciliationItem.objects.filter(reconciliation=rec,
            description__icontains="petty cash").first()
        adv = ReconciliationItem.objects.filter(reconciliation=rec,
            description__icontains="staff advance").first()
        self.assertTrue(petty and petty.auto and petty.effect == "ADD")
        self.assertTrue(adv and adv.auto and adv.amount == Decimal("7000"))

    def test_recon_managed_items_no_duplicates_and_update(self):
        from statements.models import BankReconciliation, ReconciliationItem
        a = StaffAdvance.objects.create(staff_name="B", department=self.dept,
            amount=Decimal("7000"), date_issued=dt.date(2026, 6, 3), purpose="x",
            method="BANK", from_petty_cash=False, issued_by=self.tr)
        rec = BankReconciliation.objects.create(statement_date=dt.date(2026, 6, 30),
            bank_balance=Decimal("50000"), created_by=self.tr)
        self.c.get(f"/reconciliations/{rec.id}/")
        self.c.get(f"/reconciliations/{rec.id}/")
        items = ReconciliationItem.objects.filter(reconciliation=rec,
            description__icontains="staff advance")
        self.assertEqual(items.count(), 1)
        # settle part -> managed amount updates
        Expense.objects.create(date=dt.date(2026, 6, 10), department=self.dept,
            description="settle", amount=Decimal("2000"), status="PAID",
            recorded_by=self.tr, advance=a)
        self.c.get(f"/reconciliations/{rec.id}/")
        self.assertEqual(items.first().amount, Decimal("5000"))
