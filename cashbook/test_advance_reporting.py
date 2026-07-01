"""Petty-funded advances are reported: they reduce the petty float and appear
in the advance list's outstanding summary, split from bank-funded advances."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import StaffAdvance, PettyCashTopUp
from cashbook.views import (outstanding_advances_total,
                            outstanding_bank_advances_total, _petty_balance_asof)


def _tr():
    u = User.objects.create_user("tr_ar", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class AdvanceReportingTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="Fund AR", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)
        PettyCashTopUp.objects.create(date=dt.date(2026, 5, 1),
            amount=Decimal("20000"), recorded_by=self.tr)

    def test_petty_advance_reduces_float(self):
        before = _petty_balance_asof(dt.date(2026, 6, 30))
        StaffAdvance.objects.create(staff_name="P", department=self.d,
            amount=Decimal("3000"), date_issued=dt.date(2026, 6, 1), purpose="fuel",
            method="CASH", from_petty_cash=True, issued_by=self.tr)
        after = _petty_balance_asof(dt.date(2026, 6, 30))
        self.assertEqual(before - after, Decimal("3000"))

    def test_petty_advance_in_all_not_bank_total(self):
        StaffAdvance.objects.create(staff_name="P", department=self.d,
            amount=Decimal("3000"), date_issued=dt.date(2026, 6, 1), purpose="x",
            method="CASH", from_petty_cash=True, issued_by=self.tr)
        allo = outstanding_advances_total(dt.date(2026, 6, 30))
        bank = outstanding_bank_advances_total(dt.date(2026, 6, 30))
        self.assertGreaterEqual(allo - bank, Decimal("3000"))  # counted as petty

    def test_advance_list_shows_source_split(self):
        StaffAdvance.objects.create(staff_name="P", department=self.d,
            amount=Decimal("3000"), date_issued=dt.date(2026, 6, 1), purpose="x",
            method="CASH", from_petty_cash=True, issued_by=self.tr)
        StaffAdvance.objects.create(staff_name="B", department=self.d,
            amount=Decimal("5000"), date_issued=dt.date(2026, 6, 1), purpose="y",
            method="BANK", from_petty_cash=False, issued_by=self.tr)
        body = self.c.get("/advances/").content.decode()
        self.assertIn("Outstanding (not yet accounted)", body)
        self.assertIn("reflected in the petty cash float", body)
        self.assertIn("shown in bank reconciliation", body)
        self.assertIn("Petty cash</span>", body)
        self.assertIn("Bank</span>", body)
        self.assertIn("Neither is double-counted", body)

    def test_petty_advance_in_register_movements(self):
        StaffAdvance.objects.create(staff_name="Reg", department=self.d,
            amount=Decimal("3000"), date_issued=dt.date(2026, 6, 1), purpose="x",
            method="CASH", from_petty_cash=True, issued_by=self.tr)
        body = self.c.get("/petty-cash/?start=2026-06-01&end=2026-06-30").content.decode()
        self.assertIn("Advance to Reg", body)
