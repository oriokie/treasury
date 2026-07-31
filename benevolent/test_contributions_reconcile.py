"""The dashboard's "Contributions" tile and the contributions list measure two
different things, and used to differ with nothing explaining why.

  * benevolent.services.reporting.contributions_total — money RECEIVED into the
    scheme funds. Registered in the Financial Metrics Registry and deliberately
    tied to the income statement, so it must not change.
  * benevolent.services.contributions.contributions_total — money ATTRIBUTED to
    a member (the BenevolentContribution records).

Money sits in the fund before anyone says whose it is — that is what the intake
queue holds — so the first is the larger whenever intake is outstanding. On the
demo data they read 13,400 and 12,500, and a treasurer had no way to account for
the 900.

The fix names the difference rather than hiding it: `unattributed_total()`, and
an "N not yet attributed" line on the tile linking to the intake queue. These
tests pin the identity received - unattributed == attributed, so the two figures
can never silently drift apart again.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from benevolent.models import BenevolentContribution, BenevolentScheme, SchemeMembership
from benevolent.services import contributions as contrib_svc
from benevolent.services import reporting as report_svc
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member

START, END = dt.date(2026, 1, 1), dt.date(2026, 12, 31)
WHEN = dt.date(2026, 6, 10)


def _treasurer(username="rc_tr"):
    u = User.objects.create_user(username, password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class _Scheme(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.fund = Department.objects.create(name="RcBenFund", fund_type="LOCAL",
                                              category="MINISTRY")
        self.scheme = BenevolentScheme.objects.create(
            name="Rc Scheme", code="RC", fund=self.fund, created_by=self.tr,
            status=BenevolentScheme.Status.ACTIVE)
        self.member = Member.objects.create(name="RC GIVER")
        self.mem = SchemeMembership.objects.create(
            scheme=self.scheme, member=self.member, joined_on=dt.date(2026, 1, 1),
            status=SchemeMembership.Status.ACTIVE)

    def _credit(self, amount, attribute=True):
        """A credit into the scheme fund, optionally attributed to the member."""
        t = Transaction.objects.create(
            date=WHEN, amount=Decimal(amount), direction="CREDIT", channel="BANK",
            confirmed=True, allocation_status="MANUAL", department=self.fund)
        if attribute:
            BenevolentContribution.objects.create(
                scheme=self.scheme, membership=self.mem, transaction=t,
                kind=BenevolentContribution.Kind.DUES, recorded_by=self.tr)
        return t


class ReconciliationTests(_Scheme):
    def test_with_everything_attributed_the_two_totals_agree(self):
        self._credit("1000")
        received = report_svc.contributions_total(START, END, self.scheme)
        attributed = contrib_svc.contributions_total(scheme=self.scheme,
                                                     start=START, end=END)
        self.assertEqual(received, attributed)
        self.assertEqual(report_svc.unattributed_total(START, END, self.scheme),
                         Decimal(0))

    def test_unattributed_money_is_the_difference(self):
        self._credit("1000")                      # attributed
        self._credit("250", attribute=False)      # sitting in the fund, unclaimed
        received = report_svc.contributions_total(START, END, self.scheme)
        attributed = contrib_svc.contributions_total(scheme=self.scheme,
                                                     start=START, end=END)
        self.assertEqual(received, Decimal("1250"))
        self.assertEqual(attributed, Decimal("1000"))
        self.assertEqual(report_svc.unattributed_total(START, END, self.scheme),
                         Decimal("250"))

    def test_the_identity_holds(self):
        """received - unattributed == attributed. The whole point."""
        self._credit("800")
        self._credit("125", attribute=False)
        received = report_svc.contributions_total(START, END, self.scheme)
        gap = report_svc.unattributed_total(START, END, self.scheme)
        attributed = contrib_svc.contributions_total(scheme=self.scheme,
                                                     start=START, end=END)
        self.assertEqual(received - gap, attributed)

    def test_it_never_reports_a_negative_gap(self):
        """A correction attributed to a member but reversed in the fund could
        make the attributed side the larger; "-250 not yet attributed" would be
        nonsense on a dashboard."""
        t = self._credit("500")
        t.is_reversed = True
        t.save(update_fields=["is_reversed"])
        self.assertGreaterEqual(
            report_svc.unattributed_total(START, END, self.scheme), Decimal(0))


class SchemeSummaryTests(_Scheme):
    def test_each_row_carries_its_own_unattributed_figure(self):
        self._credit("1000")
        self._credit("300", attribute=False)
        row = next(r for r in report_svc.scheme_summary(START, END)
                   if r["scheme"].pk == self.scheme.pk)
        self.assertEqual(row["contributions"], Decimal("1300"))
        self.assertEqual(row["unattributed"], Decimal("300"))

    def test_totals_add_the_unattributed_column_up(self):
        self._credit("1000")
        self._credit("300", attribute=False)
        rows = report_svc.scheme_summary(START, END)
        t = report_svc.totals(rows)
        self.assertEqual(t["unattributed"],
                         sum(r["unattributed"] for r in rows))
        self.assertEqual(t["contributions"] - t["unattributed"],
                         contrib_svc.contributions_total(start=START, end=END))

    def test_the_summary_stays_flat_as_schemes_are_added(self):
        """The attributed figure is one grouped query — computing it per scheme
        would put the dashboard back on an N+1."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self._credit("100")
        with CaptureQueriesContext(connection) as one:
            report_svc.scheme_summary(START, END)
        for i in range(3):
            f = Department.objects.create(name=f"RcExtra{i}", fund_type="LOCAL",
                                          category="MINISTRY")
            BenevolentScheme.objects.create(
                name=f"Extra {i}", code=f"EX{i}", fund=f, created_by=self.tr,
                status=BenevolentScheme.Status.ACTIVE)
        with CaptureQueriesContext(connection) as many:
            report_svc.scheme_summary(START, END)
        # a couple more for the extra funds' own figures is fine; per-scheme
        # growth of the contribution lookup is not
        self.assertLessEqual(len(many) - len(one), 6,
                             "scheme_summary grew per scheme — check for an N+1")


class DashboardTileTests(_Scheme):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.tr)

    def test_the_tile_names_the_unattributed_amount(self):
        self._credit("1000")
        self._credit("250", attribute=False)
        body = self.client.get("/benevolent/").content.decode()
        self.assertIn("not yet attributed", body)
        self.assertIn("/benevolent/intake/", body)

    def test_the_tile_stays_quiet_when_everything_is_attributed(self):
        """No dangling "0 not yet attributed" when there is nothing to chase."""
        self._credit("1000")
        body = self.client.get("/benevolent/").content.decode()
        self.assertNotIn("not yet attributed", body)

    def test_the_registered_metric_is_unchanged(self):
        """reporting.contributions_total is the registry's
        `benevolent_contributions` and ties to the income statement — this fix
        must not have altered what it measures."""
        self._credit("1000")
        self._credit("250", attribute=False)
        self.assertEqual(report_svc.contributions_total(START, END, self.scheme),
                         Decimal("1250"))
