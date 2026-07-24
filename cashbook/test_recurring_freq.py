"""Recurring expenses support monthly, quarterly and yearly cadences (#2)."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.contrib.auth.models import User

from departments.models import Department
from cashbook.models import RecurringExpense
from cashbook.services.recurring import due_dates


class RecurringFrequencyTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("rf", password="x")
        self.d = Department.objects.create(name="LCB", fund_type="LOCAL",
                                           category="OFFERING", show_in_expenses=True)

    def _sched(self, freq, start, dom=15):
        return RecurringExpense(description="x", department=self.d, amount=Decimal("100"),
            frequency=freq, day_of_month=dom, start_date=start, created_by=self.u)

    def test_quarterly_every_three_months(self):
        dd = due_dates(self._sched("QUARTERLY", dt.date(2026, 2, 1)), dt.date(2026, 12, 31))
        self.assertEqual([(d.month, d.day) for d in dd], [(2,15),(5,15),(8,15),(11,15)])

    def test_yearly_same_month(self):
        dd = due_dates(self._sched("YEARLY", dt.date(2026, 3, 1)), dt.date(2028, 12, 31))
        self.assertEqual([d.isoformat() for d in dd],
                         ["2026-03-15", "2027-03-15", "2028-03-15"])

    def test_monthly_still_works(self):
        dd = due_dates(self._sched("MONTHLY", dt.date(2026, 1, 1)), dt.date(2026, 4, 30))
        self.assertEqual(len(dd), 4)

    def test_choices_available(self):
        codes = {c[0] for c in RecurringExpense.Frequency.choices}
        self.assertTrue({"MONTHLY", "QUARTERLY", "YEARLY"}.issubset(codes))


class RecurringCarriesEveryExpenseFieldTests(TestCase):
    """A schedule must produce a complete expense, not a stub.

    Before this, a schedule held only description, fund, category, amount,
    claimant and method — so every generated row arrived missing the supplier,
    the payee, the voucher number and the budget line, and someone had to open
    each one and fill them in. A schedule that creates work is not a schedule.
    """

    def setUp(self):
        from django.contrib.auth.models import Group, User
        from core.roles import TREASURER
        from departments.models import Department
        from vendors.models import Vendor

        self.user = User.objects.create_user("sched", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Utilities", slug="utilities",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.supplier = Vendor.objects.create(name="Kenya Power")

    def _schedule(self, **kw):
        from cashbook.models import RecurringExpense
        defaults = dict(
            description="Monthly power bill", department=self.fund,
            category="UTILITIES", amount=Decimal("4500"),
            frequency=RecurringExpense.Frequency.MONTHLY, day_of_month=5,
            start_date=dt.date.today() - dt.timedelta(days=90),
            created_by=self.user, vendor=self.supplier,
            voucher_no="SO-114", paid_from_petty_cash=False)
        defaults.update(kw)
        return RecurringExpense.objects.create(**defaults)

    def test_the_generated_expense_carries_the_supplier_and_details(self):
        from cashbook.services.recurring import generate_due
        schedule = self._schedule()
        generate_due(user=self.user)

        from cashbook.models import Expense
        expense = (Expense.objects.filter(recurring=schedule)
                   .order_by("date").first())
        self.assertIsNotNone(expense, "The schedule generated nothing.")
        self.assertEqual(expense.vendor, self.supplier)
        self.assertEqual(expense.voucher_no, "SO-114")
        self.assertEqual(expense.payee, "Kenya Power",
                         "The payee should fall back to the supplier's name.")

    def test_an_explicit_payee_is_not_overwritten_by_the_supplier(self):
        from cashbook.models import Expense
        from cashbook.services.recurring import generate_due
        schedule = self._schedule(payee="KPLC Prepaid")
        generate_due(user=self.user)
        expense = Expense.objects.filter(recurring=schedule).order_by("date").first()
        self.assertEqual(expense.payee, "KPLC Prepaid")

    def test_the_expenditure_type_comes_from_the_schedule(self):
        """A monthly instalment on a capital purchase is scheduled but is not a
        recurrent cost, and calling it one misstates the analysis."""
        from cashbook.models import Expense
        from cashbook.services.recurring import generate_due
        schedule = self._schedule(expenditure_type=Expense.ExpenditureType.CAPITAL)
        generate_due(user=self.user)
        expense = Expense.objects.filter(recurring=schedule).order_by("date").first()
        self.assertEqual(expense.expenditure_type, Expense.ExpenditureType.CAPITAL)

    def test_the_form_offers_every_expense_field(self):
        from cashbook.forms import ExpenseForm, RecurringExpenseForm
        recurring = set(RecurringExpenseForm().fields)
        expense = set(ExpenseForm().fields)
        # `date` is the schedule's job (frequency + day_of_month), and
        # `capitalized_asset`/`charge` are per-payment choices a schedule cannot
        # make in advance — which asset a given month's instalment capitalises,
        # or who to charge this one back to.
        missing = expense - recurring - {"date", "capitalized_asset", "charge"}
        self.assertFalse(
            missing,
            f"The recurring form is missing expense fields: {sorted(missing)}. "
            f"A schedule that cannot record them produces incomplete rows.")
        self.assertIn("frequency", recurring)
        self.assertIn("end_date", recurring)
