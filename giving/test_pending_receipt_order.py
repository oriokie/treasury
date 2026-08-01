"""Pending receipt, in the order a treasurer reads it.

The list is worked down name by name to issue receipts, so it has to be in the
order of the Member column. It was sorted by `name_key` instead — the
order-insensitive matching key, which sorts a name's WORDS alphabetically so
that "ALAN OTIENO" and "OTIENO ALAN" are recognised as one person.

That is exactly right for deciding who sits together and wrong for deciding
where they sit: "WIDOW NYAMONGO" keys to "NYAMONGO WIDOW" and files under N
while the column shows a W. With three givers the page read

    ZAC ABALA / ALAN OTIENO / WIDOW NYAMONGO

which is Z, A, W — indistinguishable from unsorted, on every surface: the page,
the Excel, the PDF, and the PDF the Telegram bot sends.

Both jobs are still done. The key groups; the displayed name orders.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from giving.services.pending_receipt import (export_rows,
                                             pending_receipt_rows)

WHEN = dt.date(2026, 6, 1)


class _Pending(TestCase):
    """Three names whose matching key and displayed name disagree on order."""

    NAMES = ["ZAC ABALA", "WIDOW NYAMONGO", "ALAN OTIENO"]

    def setUp(self):
        self.tr = User.objects.create_user("pro_tr", password="pro-pass-1",
                                           is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="ProTithe", slug="pro-tithe",
            fund_type=Department.FundType.TRUST,
            category=Department.Category.MINISTRY)
        for i, name in enumerate(self.NAMES):
            self._gift(name, 100 + i)

    _seq = 0

    def _gift(self, payer_name, amount, day=None):
        # A distinct reference per gift on purpose: `_group_split_siblings`
        # treats same-day rows sharing a reference as two halves of ONE split
        # contribution, so without this the whole fixture collapses to a single
        # row and the ordering under test never runs.
        type(self)._seq += 1
        return Transaction.objects.create(
            date=day or WHEN, amount=Decimal(amount), direction="CREDIT",
            channel=Transaction.Channel.BANK, confirmed=True,
            allocation_status="MANUAL", department=self.fund,
            payer_name=payer_name, excluded_from_income=False,
            reference=f"PRO{type(self)._seq:03d}",
            core_ref=f"PROCORE{type(self)._seq:03d}")

    def _names(self):
        return [r[2] for r in pending_receipt_rows()]


class OrderTests(_Pending):
    def test_the_list_is_in_member_name_order(self):
        self.assertEqual(self._names(),
                         ["ALAN OTIENO", "WIDOW NYAMONGO", "ZAC ABALA"])

    def test_a_name_is_not_filed_under_its_second_word(self):
        """The bug in one line: WIDOW NYAMONGO must not sort under N."""
        names = self._names()
        self.assertLess(names.index("WIDOW NYAMONGO"), names.index("ZAC ABALA"))

    def test_the_downloads_are_in_the_same_order(self):
        """Excel, PDF and the Telegram PDF all render `export_rows`, so this is
        the one place the download order is decided."""
        self.assertEqual([r[2] for r in export_rows(pending_receipt_rows())],
                         ["ALAN OTIENO", "WIDOW NYAMONGO", "ZAC ABALA"])

    def test_the_page_is_in_the_same_order(self):
        c = Client()
        c.force_login(self.tr)
        body = c.get("/transactions/pending-receipt/").content.decode()
        positions = [body.index(n) for n in
                     ["ALAN OTIENO", "WIDOW NYAMONGO", "ZAC ABALA"]]
        self.assertEqual(positions, sorted(positions))


class GroupingStillWorksTests(_Pending):
    """The matching key kept its actual job: the same giver recorded two ways
    is one block, not two entries filed apart."""

    def test_two_spellings_of_one_giver_sit_together(self):
        self._gift("OTIENO ALAN", 500)          # same person, reversed
        names = self._names()
        first = names.index("ALAN OTIENO")
        self.assertEqual(names[first + 1], "OTIENO ALAN",
                         "the same giver's two spellings were split apart")

    def test_the_block_is_placed_by_the_earlier_spelling(self):
        """Ordering a two-spelling block by whichever row came first would put
        the same block in different places on different days."""
        self._gift("OTIENO ALAN", 500)
        names = self._names()
        self.assertEqual(names[0], "ALAN OTIENO")

    def test_a_giver_with_no_name_sorts_last(self):
        self._gift("", 900)
        self.assertEqual(self._names()[-1], "")

    def test_one_givers_rows_stay_together_across_dates(self):
        """Date is the tie-break WITHIN a giver, never above them."""
        self._gift("ALAN OTIENO", 700, day=dt.date(2026, 1, 1))
        names = self._names()
        self.assertEqual(names[:2], ["ALAN OTIENO", "ALAN OTIENO"])


class OtherSortsTests(_Pending):
    """The page's other sorts use the displayed name as the tie-break too, so
    they cannot reintroduce the same confusion within a date or a fund."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.tr)

    def _rows(self, sort):
        r = self.client.get("/transactions/pending-receipt/", {"sort": sort})
        self.assertEqual(r.status_code, 200)
        return [row["name"] for row in r.context["rows"]]

    def test_sorting_by_date_breaks_ties_on_the_displayed_name(self):
        # every row shares one date, so the tie-break is the whole order
        self.assertEqual(self._rows("date"),
                         ["ALAN OTIENO", "WIDOW NYAMONGO", "ZAC ABALA"])

    def test_sorting_by_fund_breaks_ties_on_the_displayed_name(self):
        self.assertEqual(self._rows("fund"),
                         ["ALAN OTIENO", "WIDOW NYAMONGO", "ZAC ABALA"])

    def test_name_is_still_the_default(self):
        r = self.client.get("/transactions/pending-receipt/")
        self.assertEqual(r.context["sort"], "name")
