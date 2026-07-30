"""Liabilities are not a department's spending, and a split must add to 100%.

Two faults, both of which let a figure look right while being wrong.

**The leader's expense page counted liabilities as spending.** A remittance to
the conference or a loan repayment moves money the church was holding or owed;
it is not the ministry's expenditure. The leader dashboard's spend total had
always excluded them — but the expense list beside it, that list's total, and
the download did not. So the same page disagreed with itself, and a leader
reading it saw their ministry charged with money it never spent.

**A split fund whose percentages did not total 100% misallocated silently.**
`split()` gives the last component whatever is left, which is right for
rounding — a cent that cannot be divided has to land somewhere. Applied to a
configuration error it is quite wrong: a fund set up as 40/40 put 40% in the
first department and 60% in the second. The split still summed to the whole, so
every total downstream reconciled and nothing could show that a fifth of the
collection had gone to a fund the church never chose.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase

from cashbook.models import Expense
from core import roles
from departments.models import Department, DepartmentLeadership
from giving.models import SplitComponent, SplitFund


class LeaderExpensesExcludeLiabilitiesTests(TestCase):

    def setUp(self):
        self.office = User.objects.create_user("tess-lead", password="office-pass-1")
        self.office.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Youth Ministry", slug="youth-lead",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.leader = User.objects.create_user("leader-lead", password="leader-pass-1")
        self.leader.groups.add(Group.objects.get_or_create(name=roles.LEADER)[0])
        DepartmentLeadership.objects.create(user=self.leader, department=self.fund)

        self.today = dt.date.today()
        self.spend = Expense.objects.create(
            date=self.today, department=self.fund, description="Chairs for the hall",
            amount=Decimal("5000"), category=Expense.Category.MATERIALS,
            status=Expense.Status.PAID, recorded_by=self.office)
        self.remittance = Expense.objects.create(
            date=self.today, department=self.fund, description="Conference remittance",
            amount=Decimal("25000"), category=Expense.Category.REMITTANCE,
            status=Expense.Status.PAID, recorded_by=self.office)

        self.client = Client()
        self.client.force_login(self.leader)

    def _page(self, **params):
        response = self.client.get(
            f"/leader/department/{self.fund.pk}/expenses/", params)
        self.assertEqual(response.status_code, 200)
        return response

    def test_the_fixture_really_does_split_the_two(self):
        """Guards the guard: if classification changed, this test proves nothing."""
        self.remittance.refresh_from_db()
        self.assertEqual(self.remittance.doc_class, Expense.DocClass.LIABILITY)
        self.spend.refresh_from_db()
        self.assertEqual(self.spend.doc_class, Expense.DocClass.EXPENSE)

    def test_a_real_expense_is_listed(self):
        self.assertIn("Chairs for the hall", self._page().content.decode())

    def test_a_liability_is_not_listed(self):
        self.assertNotIn("Conference remittance", self._page().content.decode())

    def test_the_total_is_the_departments_own_spending(self):
        """The figure a leader reads off the page, and reports to a committee."""
        self.assertEqual(self._page().context["total"], Decimal("5000"))

    def test_the_count_matches_the_list(self):
        self.assertEqual(self._page().context["count"], 1)

    def test_the_download_agrees_with_the_page(self):
        """A leader who exports and totals the column must get the same answer."""
        raw = self.client.get(
            f"/leader/department/{self.fund.pk}/expenses/",
            {"export": "csv"}).content.decode()
        self.assertIn("Chairs for the hall", raw)
        self.assertNotIn("Conference remittance", raw)

    def test_the_dashboard_and_the_page_now_agree(self):
        """The inconsistency that made this a bug rather than a preference.

        The dashboard total already excluded liabilities. The page beside it did
        not, so the same department had two different spend figures depending on
        which screen you were looking at.
        """
        # A leader of a single department is sent straight to it, so follow
        # through rather than asserting on the redirect.
        # A leader of a single department is sent straight to it, so follow
        # through rather than asserting on the redirect. What matters is that no
        # screen in the leader area presents the remittance as spending — the
        # department page composes its own recent-expenses list, and that list
        # was one of the three places missing the exclusion.
        dashboard = self.client.get("/leader/", follow=True)
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn("Conference remittance", dashboard.content.decode())
        self.assertEqual(self._page().context["total"], Decimal("5000"))

    def test_a_loan_repayment_is_treated_the_same_way(self):
        """Remittance is not a special case — the rule is the document class."""
        Expense.objects.create(
            date=self.today, department=self.fund, description="Loan instalment",
            amount=Decimal("9000"), category=Expense.Category.LOAN_REPAYMENT,
            status=Expense.Status.PAID, recorded_by=self.office)
        self.assertEqual(self._page().context["total"], Decimal("5000"))


class SplitFundsMustAddToTheWholeTests(TestCase):

    def setUp(self):
        self.a = Department.objects.create(
            name="Trust Half", slug="trust-half", is_trust=True,
            fund_type=Department.FundType.TRUST,
            category=Department.Category.TRUST)
        self.b = Department.objects.create(
            name="Local Half", slug="local-half",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.split = SplitFund.objects.create(name="Combined Offering")

    def _components(self, first, second):
        self.split.components.all().delete()
        SplitComponent.objects.create(split_fund=self.split, department=self.a,
                                      percent=Decimal(first))
        SplitComponent.objects.create(split_fund=self.split, department=self.b,
                                      percent=Decimal(second))
        return SplitFund.objects.get(pk=self.split.pk)

    def test_a_correct_split_divides_as_configured(self):
        rows = self._components("50", "50").split(Decimal("1000"))
        self.assertEqual([amount for _dept, amount in rows],
                         [Decimal("500.00"), Decimal("500.00")])

    def test_an_uneven_amount_still_sums_to_the_whole(self):
        """A cent that cannot be divided has to land somewhere."""
        rows = self._components("50", "50").split(Decimal("100.01"))
        self.assertEqual(sum(amount for _dept, amount in rows), Decimal("100.01"))

    def test_a_split_that_does_not_total_a_hundred_is_refused(self):
        with self.assertRaises(ValidationError):
            self._components("40", "40").split(Decimal("1000"))

    def test_the_refusal_names_the_fund_and_the_shortfall(self):
        """A treasurer needs to know which split and by how much."""
        try:
            self._components("40", "40").split(Decimal("1000"))
            self.fail("A misconfigured split allocated money.")
        except ValidationError as exc:
            message = " ".join(exc.messages)
            self.assertIn("Combined Offering", message)
            self.assertIn("80", message)

    def test_over_a_hundred_is_refused_too(self):
        with self.assertRaises(ValidationError):
            self._components("60", "60").split(Decimal("1000"))

    def test_correcting_the_split_makes_it_work(self):
        """The difference is a configuration error, not a fault in the split."""
        with self.assertRaises(ValidationError):
            self._components("40", "40").split(Decimal("1000"))
        rows = self._components("40", "60").split(Decimal("1000"))
        self.assertEqual([amount for _dept, amount in rows],
                         [Decimal("400.00"), Decimal("600.00")])

    def test_a_split_with_no_components_returns_nothing(self):
        """Nothing configured is not the same as configured wrongly."""
        self.split.components.all().delete()
        self.assertEqual(SplitFund.objects.get(pk=self.split.pk)
                         .split(Decimal("1000")), [])

    def test_three_way_splits_are_allowed(self):
        self.split.components.all().delete()
        third = Department.objects.create(
            name="Third Share", slug="third-share",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        for dept, pct in ((self.a, "33.33"), (self.b, "33.33"), (third, "33.34")):
            SplitComponent.objects.create(split_fund=self.split, department=dept,
                                          percent=Decimal(pct))
        rows = SplitFund.objects.get(pk=self.split.pk).split(Decimal("100"))
        self.assertEqual(sum(amount for _dept, amount in rows), Decimal("100"))
