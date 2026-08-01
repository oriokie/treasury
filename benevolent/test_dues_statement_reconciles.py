"""A member's statement has to add up to the figure printed above it.

`arrears_for()` is the single definition of what a member owes, and its rule is
that what counts is the money, not how the payment happened to be labelled — a
lump sum clears the months it covers. The period-by-period statement underneath
it was matching payments to periods by `period_label` alone, so a dues receipt
entered without a period was counted by the headline and by nothing else.

What the member saw, with 1,400 paid against 1,600 due:

    OWING NOW   KSh 200.00
    2026-07     due 200   paid 0   KSh 200.00 outstanding
    2026-08     due 200   paid 0   KSh 200.00 outstanding

Two debts under a headline naming one, and the payment they had actually made
appeared on no row at all. These tests pin the two together.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from benevolent.models import (BenevolentContribution, BenevolentScheme,
                               SchemeMembership, SchemePolicy)
from benevolent.services import contributions as cs
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member

AS_OF = dt.date(2026, 8, 1)


class _DuesScheme(TestCase):
    """A monthly-dues scheme of 200, member covered from 1 Jan 2026."""

    def setUp(self):
        self.tr = User.objects.create_user("ds_tr", password="x", is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(name="DsFund", fund_type="LOCAL",
                                              category="MINISTRY")
        self.scheme = BenevolentScheme.objects.create(
            name="Ds Scheme", code="DS", fund=self.fund, created_by=self.tr,
            status=BenevolentScheme.Status.ACTIVE)
        self.policy = SchemePolicy.objects.create(
            scheme=self.scheme, version=1, effective_from=dt.date(2026, 1, 1),
            status=SchemePolicy.Status.ACTIVE,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            created_by=self.tr)
        self.member = Member.objects.create(name="DS MEMBER")
        self.mem = SchemeMembership.objects.create(
            scheme=self.scheme, member=self.member, joined_on=dt.date(2026, 1, 1),
            status=SchemeMembership.Status.ACTIVE)

    def _pay(self, day, amount="200", period_label=""):
        t = Transaction.objects.create(
            date=day, amount=Decimal(amount), direction="CREDIT", channel="BANK",
            confirmed=True, allocation_status="MANUAL", department=self.fund)
        return BenevolentContribution.objects.create(
            scheme=self.scheme, membership=self.mem, transaction=t,
            kind=BenevolentContribution.Kind.DUES,
            period_label=period_label, recorded_by=self.tr)

    def _rows(self):
        return cs.dues_schedule(self.mem, self.policy, as_of=AS_OF)

    def _table_outstanding(self):
        return sum((r["outstanding"] for r in self._rows()), Decimal(0))

    def _arrears(self):
        return cs.arrears_for(self.mem, self.policy, as_of=AS_OF)


class StatementReconcilesTests(_DuesScheme):
    def test_an_unlabelled_payment_settles_a_period(self):
        """The reported case: six labelled months, one payment with no period."""
        for month in range(1, 7):
            self._pay(dt.date(2026, month, 8), period_label=f"2026-{month:02d}")
        self._pay(dt.date(2026, 7, 19))                 # no period named
        rows = {r["period"]: r for r in self._rows()}
        self.assertEqual(rows["2026-07"]["outstanding"], Decimal(0),
                         "a month the member had paid for still showed as owed")
        self.assertEqual(rows["2026-08"]["outstanding"], Decimal("200"))

    def test_the_table_totals_to_the_headline(self):
        for month in range(1, 7):
            self._pay(dt.date(2026, month, 8), period_label=f"2026-{month:02d}")
        self._pay(dt.date(2026, 7, 19))
        self.assertEqual(self._table_outstanding(), self._arrears(),
                         "the statement and 'owing now' disagree")

    def test_a_lump_sum_clears_several_months(self):
        """arrears_for has always said a lump sum clears the months it covers;
        the statement now agrees instead of showing every month unpaid."""
        self._pay(dt.date(2026, 3, 1), amount="600")    # three months at once
        rows = {r["period"]: r for r in self._rows()}
        for period in ("2026-01", "2026-02", "2026-03"):
            self.assertEqual(rows[period]["outstanding"], Decimal(0), period)
        self.assertEqual(rows["2026-04"]["outstanding"], Decimal("200"))
        self.assertEqual(self._table_outstanding(), self._arrears())

    def test_it_settles_the_oldest_period_first(self):
        """An arrears account is cleared oldest first — and it is the oldest
        missed month that decides whether a member's record is unbroken."""
        self._pay(dt.date(2026, 5, 1))                  # one unlabelled payment
        rows = self._rows()
        self.assertEqual(rows[0]["period"], "2026-01")
        self.assertEqual(rows[0]["outstanding"], Decimal(0),
                         "an unallocated payment skipped the oldest debt")
        self.assertEqual(rows[1]["outstanding"], Decimal("200"))

    def test_a_settled_row_says_where_the_money_came_from(self):
        """A period shown as paid that no receipt names would be its own
        confusion, so the row carries the amount for the template to explain."""
        self._pay(dt.date(2026, 7, 19))
        row = next(r for r in self._rows() if r["period"] == "2026-01")
        self.assertEqual(row.get("from_unallocated"), Decimal("200"))

    def test_labelled_payments_are_untouched(self):
        """No unallocated money, no change in behaviour."""
        for month in range(1, 9):
            self._pay(dt.date(2026, month, 1), period_label=f"2026-{month:02d}")
        for r in self._rows():
            self.assertEqual(r["outstanding"], Decimal(0))
            self.assertNotIn("from_unallocated", r)
        self.assertEqual(self._arrears(), Decimal(0))

    def test_overpayment_does_not_create_a_credit_row(self):
        """More unallocated money than is owed clears everything and stops —
        it must not drive a period negative."""
        self._pay(dt.date(2026, 2, 1), amount="99999")
        for r in self._rows():
            self.assertGreaterEqual(r["outstanding"], Decimal(0))
        self.assertEqual(self._table_outstanding(), Decimal(0))
        self.assertEqual(self._arrears(), Decimal(0))

    def test_a_levy_still_does_not_settle_dues(self):
        """The rule this must not break: only dues pay off dues. A levy with no
        period label must not be swept up as a subscription payment."""
        t = Transaction.objects.create(
            date=dt.date(2026, 3, 1), amount=Decimal("5000"), direction="CREDIT",
            channel="BANK", confirmed=True, allocation_status="MANUAL",
            department=self.fund)
        BenevolentContribution.objects.create(
            scheme=self.scheme, membership=self.mem, transaction=t,
            kind=BenevolentContribution.Kind.LEVY, recorded_by=self.tr)
        self.assertEqual(self._table_outstanding(), Decimal("1600"),
                         "a levy cleared the member's dues")
        self.assertEqual(self._table_outstanding(), self._arrears())

    def test_a_reversed_payment_settles_nothing(self):
        c = self._pay(dt.date(2026, 7, 19))
        c.transaction.is_reversed = True
        c.transaction.save(update_fields=["is_reversed"])
        self.assertEqual(self._table_outstanding(), Decimal("1600"))
        self.assertEqual(self._table_outstanding(), self._arrears())


class TheSameWindowTests(_DuesScheme):
    """`arrears_for` bounds what it counts to the member's own dues window;
    the statement did not, so money outside that window cleared a month in the
    table while still being owed in the headline directly above it.

    Both now read the same dates. The rule itself is unchanged — it is
    `arrears_for`'s, and it is the one the eligibility engine and the arrears
    deduction on a payout already obey.
    """

    def test_a_payment_dated_after_the_reporting_date_is_not_spent_yet(self):
        """As at 1 August, money banked on the 8th has not been received."""
        for month in range(1, 8):
            self._pay(dt.date(2026, month, 1), period_label=f"2026-{month:02d}")
        self._pay(dt.date(2026, 8, 8), period_label="2026-08")   # future
        rows = {r["period"]: r for r in self._rows()}
        self.assertEqual(rows["2026-08"]["outstanding"], Decimal("200"),
                         "a future-dated receipt cleared a month early")
        self.assertEqual(self._table_outstanding(), self._arrears())

    def test_a_payment_made_before_cover_began_is_treated_alike_by_both(self):
        """Whatever the answer, the table and the headline must give the same
        one — that is the whole point of there being a single definition."""
        self.mem.joined_on = dt.date(2026, 4, 1)
        self.mem.save(update_fields=["joined_on"])
        self._pay(dt.date(2026, 1, 15), amount="400")            # before cover
        self.assertEqual(self._table_outstanding(), self._arrears())

    def test_a_member_who_has_left_stops_counting_at_the_leaving_date(self):
        self.mem.left_on = dt.date(2026, 5, 31)
        self.mem.status = SchemeMembership.Status.WITHDRAWN
        self.mem.save(update_fields=["left_on", "status"])
        self._pay(dt.date(2026, 7, 1), amount="1000")            # after leaving
        self.assertEqual(self._table_outstanding(), self._arrears())

    def test_the_windowing_did_not_cost_an_extra_query(self):
        """The bound goes on the aggregate that was already being issued."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for month in range(1, 7):
            self._pay(dt.date(2026, month, 1), period_label=f"2026-{month:02d}")
        with CaptureQueriesContext(connection) as ctx:
            self._rows()
        contrib = [q for q in ctx.captured_queries
                   if "benevolent_benevolentcontribution" in q["sql"]
                   and "period_label" in q["sql"]]
        self.assertEqual(len(contrib), 1, "the per-period totals cost >1 query")


class BatchPathAgreesTests(_DuesScheme):
    """`standing.facts_for_scheme` warms a per-member cache and then runs the
    same code. Both paths have to give one answer, or the arrears report and the
    member's own statement would contradict each other."""

    def _batched_rows(self):
        """The rows as the batch pass computes them, caches and all."""
        from benevolent.services import standing as st
        pairs = st.facts_for_scheme(self.scheme, as_of=AS_OF)
        warmed = next(m for m, _ in pairs if m.pk == self.mem.pk)
        return cs.dues_schedule(warmed, self.policy, as_of=AS_OF)

    def test_the_batch_pass_reaches_the_same_figure(self):
        for month in range(1, 7):
            self._pay(dt.date(2026, month, 8), period_label=f"2026-{month:02d}")
        self._pay(dt.date(2026, 7, 19))
        direct = self._table_outstanding()
        cached = sum((r["outstanding"] for r in self._batched_rows()), Decimal(0))
        self.assertEqual(cached, direct)

    def test_the_batch_pass_windows_by_date_too(self):
        """The warmed per-period cache is built in Python rather than by the
        query above, so it is a second place the window has to be applied — and
        the arrears report reads it while the member's own page does not."""
        for month in range(1, 8):
            self._pay(dt.date(2026, month, 1), period_label=f"2026-{month:02d}")
        self._pay(dt.date(2026, 8, 8), period_label="2026-08")   # future
        rows = {r["period"]: r for r in self._batched_rows()}
        self.assertEqual(rows["2026-08"]["outstanding"], Decimal("200"),
                         "the batch path spent a future-dated receipt")

    def test_the_batch_pass_spends_unallocated_money_too(self):
        self._pay(dt.date(2026, 5, 1), amount="400")             # no period
        rows = self._batched_rows()
        self.assertEqual(rows[0]["outstanding"], Decimal(0))
        self.assertEqual(rows[1]["outstanding"], Decimal(0))
        self.assertEqual(rows[2]["outstanding"], Decimal("200"))
