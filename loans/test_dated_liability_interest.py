"""The Loan Liability Schedule's interest column must honour its as-at date.

The schedule computes principal with outstanding_asof(as_of) but used to take
interest from Loan.outstanding_interest, an undated property that accrues to
today and nets off every interest payment ever recorded. A schedule 'as at' an
earlier date therefore reported today's interest — a loan taken 800 days ago
and never repaid showed the same figure whatever date you asked for — so every
prior-period and comparative schedule overstated the liability. These tests
pin both halves of the figure (the accrual AND the payments netted off it) to
the requested date, and check the report totals built from these rows follow.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from ledger.services import posting
from loans.models import Lender, Loan
from loans.services import loans as svc, reporting


def _user(name, role=TREASURER):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class DatedInterestTests(TestCase):
    """100,000 borrowed 800 days ago at 12% simple, never repaid: 12% of
    100,000 is 12,000 a year, so the accrual is a round 32.876712/day and the
    arithmetic below is easy to check by hand."""

    def setUp(self):
        posting.ensure_chart()
        self.tr = _user("di_tr")
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        self.lender = Lender.objects.create(name="ACME SACCO")
        self.today = dt.date.today()
        self.start = self.today - dt.timedelta(days=800)
        self.half = self.today - dt.timedelta(days=400)
        self.loan = Loan.objects.create(lender=self.lender, fund=self.fund,
                                        loan_date=self.start,
                                        principal_amount=Decimal("100000"),
                                        interest_rate=Decimal("12"),
                                        interest_method="SIMPLE",
                                        created_by=self.tr)
        svc.record_receipt(self.loan, date=self.start,
                           amount=Decimal("100000"), user=self.tr)

    def _row(self, as_of):
        rows = reporting.liability_schedule(as_of=as_of)
        return next(r for r in rows if r["loan"].pk == self.loan.pk)

    def test_interest_as_at_an_earlier_date_is_the_interest_of_that_date(self):
        """400 days of accrual, not 800 — the whole defect in one assertion."""
        row = self._row(self.half)
        self.assertEqual(row["outstanding_interest"], Decimal("13150.68"))
        self.assertEqual(row["outstanding_principal"], Decimal("100000"))
        self.assertEqual(row["total_outstanding"], Decimal("113150.68"))

    def test_the_schedule_still_reports_todays_interest_as_at_today(self):
        """The fix must not shift the default view: with no as-of date the
        schedule is 'as at today' and has to be unchanged."""
        self.assertEqual(self._row(self.today)["outstanding_interest"],
                         Decimal("26301.37"))
        self.assertEqual(reporting.liability_schedule()[0]["outstanding_interest"],
                         Decimal("26301.37"))

    def test_an_interest_payment_is_only_netted_off_once_it_has_been_made(self):
        """The other half of the bug: interest_paid summed every payment ever,
        so a payment made last month reduced the liability shown for last
        year — a date the church had not paid it on."""
        svc.record_interest(self.loan, date=self.today - dt.timedelta(days=100),
                            amount=Decimal("5000"), user=self.tr)
        self.assertEqual(self._row(self.half)["outstanding_interest"],
                         Decimal("13150.68"))          # not yet paid on that date
        self.assertEqual(self._row(self.today)["outstanding_interest"],
                         Decimal("21301.37"))          # 26,301.37 less the 5,000

    def test_the_report_totals_follow_the_dated_rows(self):
        """reports/loan_reports.py sums these row values, so the schedule page
        and its export must show the dated figure without a change of their
        own — this is the assertion that says the fix reached the report."""
        self.client.force_login(self.tr)
        url = reverse("report_loan_liability")
        r = self.client.get(f"{url}?as_of={self.half.isoformat()}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["total_interest"], Decimal("13150.68"))
        self.assertEqual(r.context["total"], Decimal("113150.68"))
        csv = self.client.get(
            f"{url}?as_of={self.half.isoformat()}&export=csv").content.decode()
        self.assertIn("13150.68", csv)
        self.assertNotIn("26301.37", csv)
