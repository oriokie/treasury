"""The member register, as a register of givers.

The page calls itself "Everyone who has given, however they were recorded" and
then showed no giving at all — not a total, not a date. The two questions a
treasurer brings to it, who gives most and who has stopped, could only be
answered by opening every member in turn.

Two other holes closed here, both of the same kind — a thing the page could
change but not show:

  * `active` could be set in BULK from this very screen, and there was no
    column, no filter and no default. A member made inactive simply vanished
    into the same undifferentiated list, with no way to find them again.
  * `member_type` was already in the page's context, used only to populate the
    bulk-edit dropdown. You could set it and not filter by it.

And the CSV export ignored the filters, which was harmless while there were
only two of them and becomes a trap once the page can be narrowed and sorted.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member

TODAY = dt.date(2026, 6, 1)


class _Register(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ml_tr", password="ml-pass-1",
                                             is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="MlFund", slug="ml-fund", fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)

        self.big = Member.objects.create(name="BIG GIVER", phone="254700000101",
                                         member_type=Member.MemberType.MEMBER)
        self.small = Member.objects.create(name="SMALL GIVER", phone="254700000102",
                                           member_type=Member.MemberType.SS_MEMBER)
        self.never = Member.objects.create(name="NEVER GIVEN", phone="254700000103")
        self.gone = Member.objects.create(name="GONE AWAY", phone="254700000104",
                                          active=False)

        self._gift(self.big, "5000", dt.date(2026, 5, 1))
        self._gift(self.big, "3000", dt.date(2026, 5, 20))
        self._gift(self.small, "500", dt.date(2026, 1, 10))

        self.client = Client()
        self.client.force_login(self.user)

    def _gift(self, member, amount, date, **kw):
        fields = dict(date=date, amount=Decimal(amount), direction="CREDIT",
                      channel="BANK", confirmed=True, allocation_status="MANUAL",
                      department=self.fund, member=member)
        fields.update(kw)
        return Transaction.objects.create(**fields)

    def _rows(self, **params):
        response = self.client.get("/members/", params)
        self.assertEqual(response.status_code, 200)
        return response, {m.name: m for m in response.context["members"]}


class GivingIsShownTests(_Register):
    def test_each_member_carries_their_total(self):
        _, rows = self._rows()
        self.assertEqual(rows["BIG GIVER"].total_given, Decimal("8000"))
        self.assertEqual(rows["SMALL GIVER"].total_given, Decimal("500"))

    def test_each_member_carries_their_last_gift(self):
        _, rows = self._rows()
        self.assertEqual(rows["BIG GIVER"].last_gift, dt.date(2026, 5, 20))

    def test_a_member_who_has_never_given_is_not_hidden(self):
        """They are exactly who a treasurer is looking for."""
        _, rows = self._rows()
        self.assertIsNone(rows["NEVER GIVEN"].total_given)
        self.assertIsNone(rows["NEVER GIVEN"].last_gift)

    def test_never_given_reads_as_never(self):
        response, _ = self._rows()
        self.assertContains(response, "Never")

    def test_a_reversed_gift_is_not_counted(self):
        """The figure has to mean what it means everywhere else — the registry's
        own definition of a counted credit, not "any row with an amount"."""
        self._gift(self.small, "9999", TODAY, is_reversed=True)
        _, rows = self._rows()
        self.assertEqual(rows["SMALL GIVER"].total_given, Decimal("500"))

    def test_an_unconfirmed_gift_is_not_counted(self):
        self._gift(self.small, "9999", TODAY, confirmed=False)
        _, rows = self._rows()
        self.assertEqual(rows["SMALL GIVER"].total_given, Decimal("500"))

    def test_the_totals_describe_the_filter_not_the_page(self):
        response, _ = self._rows()
        self.assertEqual(response.context["sum_members"], 3)   # active only
        self.assertEqual(response.context["sum_given"], Decimal("8500"))

    def test_the_totals_follow_a_filter(self):
        response, _ = self._rows(type=Member.MemberType.MEMBER)
        self.assertEqual(response.context["sum_members"], 1)
        self.assertEqual(response.context["sum_given"], Decimal("8000"))

    def test_it_stays_one_query_as_the_register_grows(self):
        """A total per member fetched in the template would be a query per
        member, and this is the page that lists every member there is."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.get("/members/")                    # warm
        with CaptureQueriesContext(connection) as before:
            self.client.get("/members/")
        for i in range(15):
            m = Member.objects.create(name=f"EXTRA GIVER {i:02d}")
            self._gift(m, "100", TODAY)
        with CaptureQueriesContext(connection) as after:
            self.client.get("/members/")
        self.assertLessEqual(
            len(after.captured_queries) - len(before.captured_queries), 1,
            "the member list grows a query per member")


class SortingTests(_Register):
    def _order(self, **params):
        _, _rows = self._rows(**params)
        response = self.client.get("/members/", params)
        return [m.name for m in response.context["members"]]

    def test_default_is_alphabetical(self):
        self.assertEqual(self._order()[0], "BIG GIVER")

    def test_most_given_first(self):
        order = self._order(sort="given")
        self.assertEqual(order[:2], ["BIG GIVER", "SMALL GIVER"])

    def test_a_member_who_never_gave_sorts_last_not_first(self):
        """On a NULL they would otherwise head the list, which is the one place
        they are least useful."""
        self.assertEqual(self._order(sort="given")[-1], "NEVER GIVEN")

    def test_longest_since_giving_puts_the_quietest_first(self):
        order = self._order(sort="quiet")
        self.assertEqual(order[0], "SMALL GIVER")     # Jan, vs May
        self.assertEqual(order[-1], "NEVER GIVEN")

    def test_gave_most_recently_is_the_other_way_round(self):
        self.assertEqual(self._order(sort="recent")[0], "BIG GIVER")

    def test_an_unknown_sort_falls_back_rather_than_erroring(self):
        response = self.client.get("/members/", {"sort": "; drop table"})
        self.assertEqual(response.status_code, 200)


class StatusTests(_Register):
    def test_inactive_members_are_out_of_the_way_by_default(self):
        _, rows = self._rows()
        self.assertNotIn("GONE AWAY", rows)

    def test_but_they_can_be_found(self):
        _, rows = self._rows(status="inactive")
        self.assertEqual(list(rows), ["GONE AWAY"])

    def test_and_both_can_be_seen_at_once(self):
        _, rows = self._rows(status="all")
        self.assertIn("GONE AWAY", rows)
        self.assertIn("BIG GIVER", rows)

    def test_an_inactive_member_is_marked_as_such(self):
        response, _ = self._rows(status="all")
        self.assertContains(response, "Inactive")

    def test_the_count_of_inactive_members_is_always_offered(self):
        response, _ = self._rows()
        self.assertEqual(response.context["sum_inactive"], 1)


class TypeFilterTests(_Register):
    def test_members_can_be_filtered_by_type(self):
        _, rows = self._rows(type=Member.MemberType.SS_MEMBER)
        self.assertEqual(list(rows), ["SMALL GIVER"])

    def test_the_type_filter_is_offered_on_the_page(self):
        response, _ = self._rows()
        self.assertContains(response, 'name="type"')


class ExportFollowsTheFilterTests(_Register):
    def _csv(self, **params):
        response = self.client.get("/members/export/", params)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_export_honours_a_filter(self):
        body = self._csv(type=Member.MemberType.SS_MEMBER)
        self.assertIn("SMALL GIVER", body)
        self.assertNotIn("BIG GIVER", body)

    def test_the_export_honours_the_sort(self):
        body = self._csv(sort="given")
        self.assertLess(body.index("BIG GIVER"), body.index("SMALL GIVER"))

    def test_the_export_defaults_to_active_members(self):
        self.assertNotIn("GONE AWAY", self._csv())

    def test_it_still_round_trips_through_the_importer(self):
        """Giving figures are deliberately NOT added to this file: a column the
        importer does not know would break re-import, which is what the export
        is for."""
        header = self._csv().splitlines()[0]
        self.assertEqual(
            header,
            "id,name,phone,group,member_type,dev_group_number,active")


class MetricPrefixTests(TestCase):
    def test_the_canonical_filter_can_be_reached_through_a_relation(self):
        """`prefix` is what lets the member list annotate with the registry's
        own definition instead of restating it — a fourth copy of the rule is
        exactly what that function exists to prevent."""
        from core.metrics import income_credit_filter
        plain = str(income_credit_filter())
        prefixed = str(income_credit_filter(prefix="transaction__"))
        self.assertIn("confirmed", plain)
        self.assertNotIn("transaction__", plain)
        self.assertIn("transaction__confirmed", prefixed)
        self.assertIn("transaction__is_reversed", prefixed)

    def test_the_dates_are_prefixed_too(self):
        from core.metrics import income_credit_filter
        q = str(income_credit_filter(start=dt.date(2026, 1, 1),
                                     end=dt.date(2026, 12, 31),
                                     prefix="transaction__"))
        self.assertIn("transaction__date__gte", q)
        self.assertIn("transaction__date__lte", q)
