"""Functional review finding: no form rejected a wildly future-dated entry, so
a simple year typo (2036 for 2026) would silently misfile a transaction with
no error — it just wouldn't show up in any current report until that future
date arrived. A small grace window (1 day, for timezone slack) now catches
genuine mistakes without blocking legitimate same-day entry."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase
from departments.models import Department


class ExpenseFutureDateTests(TestCase):
    def setUp(self):
        self.d = Department.objects.create(name="FutureDateFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_far_future_date_rejected(self):
        from cashbook.forms import ExpenseForm
        form = ExpenseForm(data={"date": "2036-06-01", "department": str(self.d.id),
            "description": "typo year", "amount": "500", "category": "OTHER",
            "method": "CASH"})
        self.assertFalse(form.is_valid())
        self.assertIn("date", form.errors)

    def test_today_still_valid(self):
        from cashbook.forms import ExpenseForm
        today = dt.date.today().isoformat()
        form = ExpenseForm(data={"date": today, "department": str(self.d.id),
            "description": "today", "amount": "500", "category": "OTHER",
            "method": "CASH"})
        self.assertNotIn("date", form.errors)

    def test_normal_past_date_still_valid(self):
        from cashbook.forms import ExpenseForm
        form = ExpenseForm(data={"date": "2026-06-01", "department": str(self.d.id),
            "description": "past", "amount": "500", "category": "OTHER",
            "method": "CASH"})
        self.assertNotIn("date", form.errors)


class FundTransferFutureDateTests(TestCase):
    def setUp(self):
        self.d1 = Department.objects.create(name="TransferFrom", fund_type="LOCAL",
            category="MINISTRY")
        self.d2 = Department.objects.create(name="TransferTo", fund_type="LOCAL",
            category="MINISTRY")

    def test_far_future_date_rejected(self):
        from cashbook.forms import FundTransferForm
        form = FundTransferForm(data={"date": "2040-01-01", "source": self.d1.id,
            "destination": self.d2.id, "amount": "1000"})
        self.assertFalse(form.is_valid())


class CashEntryFutureDateTests(TestCase):
    def setUp(self):
        self.d = Department.objects.create(name="CashFutureFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_far_future_date_rejected(self):
        from giving.forms import CashEntryForm
        form = CashEntryForm(data={"date": "2099-01-01", "channel": "CASH",
            "fund": f"d:{self.d.id}", "amount": "100"})
        self.assertFalse(form.is_valid())

    def test_normal_date_still_valid(self):
        from giving.forms import CashEntryForm
        form = CashEntryForm(data={"date": "2026-06-01", "channel": "CASH",
            "fund": f"d:{self.d.id}", "amount": "100"})
        self.assertNotIn("date", form.errors)


class TransactionEditFutureDateTests(TestCase):
    def test_far_future_date_rejected(self):
        from giving.forms import TransactionEditForm
        d = Department.objects.create(name="EditFutureFund", fund_type="LOCAL",
            category="MINISTRY")
        form = TransactionEditForm(data={"date": "2050-01-01", "channel": "CASH",
            "direction": "CREDIT", "department": d.id, "amount": "500",
            "allocation_status": "MANUAL"})
        self.assertFalse(form.is_valid())
        self.assertIn("date", form.errors)


class ExpenseFormInitIntegrityTests(TestCase):
    """Regression guard: ExpenseForm.__init__ must fully complete its setup
    (accessibility attributes, capitalized_asset queryset restriction, the
    paid_from_petty_cash label/help text) — a previous edit accidentally left
    the tail of __init__ as dead code after an inserted clean_date() method,
    silently disabling all of it with no visible error."""
    def setUp(self):
        self.d = Department.objects.create(name="InitIntegrityFund", fund_type="LOCAL",
            category="MINISTRY")

    def test_style_applied_aria_required_present(self):
        from cashbook.forms import ExpenseForm
        form = ExpenseForm()
        self.assertEqual(form.fields["description"].widget.attrs.get("aria-required"), "true")

    def test_capitalized_asset_queryset_restricted_to_not_disposed(self):
        from cashbook.forms import ExpenseForm
        from assets.models import FixedAsset
        disposed = FixedAsset.objects.create(name="Old asset", disposed=True,
            acquired_on=dt.date(2020, 1, 1), cost=Decimal("1000"))
        live = FixedAsset.objects.create(name="Current asset", disposed=False,
            acquired_on=dt.date(2024, 1, 1), cost=Decimal("2000"))
        form = ExpenseForm()
        qs_ids = set(form.fields["capitalized_asset"].queryset.values_list("id", flat=True))
        self.assertIn(live.id, qs_ids)
        self.assertNotIn(disposed.id, qs_ids)

    def test_paid_from_petty_cash_help_text_set(self):
        from cashbook.forms import ExpenseForm
        form = ExpenseForm()
        self.assertIn("petty cash float",
                      form.fields["paid_from_petty_cash"].help_text.lower())

    def test_expenditure_type_initial_set(self):
        from cashbook.forms import ExpenseForm
        from cashbook.models import Expense
        form = ExpenseForm()
        self.assertEqual(form.fields["expenditure_type"].initial,
                         Expense.ExpenditureType.RECURRENT)

    def test_form_renders_aria_required_on_page(self):
        from django.contrib.auth.models import User, Group
        from django.test import Client
        u = User.objects.create_user("tr_initint", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        c = Client(); c.force_login(u)
        b = c.get("/expenses/new/").content.decode()
        self.assertIn('aria-required="true"', b)
